
__author__ = 'Bohdan Mushkevych'

from db.error import DuplicateKeyError
from datetime import datetime
from logging import ERROR, WARNING, INFO

from scheduler.scheduler_constants import PIPELINE_DISCRETE
from scheduler.abstract_pipeline import AbstractPipeline
from db.model import job, unit_of_work
from db.model.unit_of_work import UnitOfWork
from db.model.worker_mq_request import WorkerMqRequest
from system import time_helper
from system.decorator import with_reconnect
from system.process_context import ProcessContext


class DiscretePipeline(AbstractPipeline):
    """ Pipeline to handle discrete timeperiod boundaries for batch jobs
    in comparison to RegularPipeline this one does not re-compute processing boundaries"""

    def __init__(self, logger, timetable, name=PIPELINE_DISCRETE):
        super(DiscretePipeline, self).__init__(logger, timetable, name)

    def __del__(self):
        super(DiscretePipeline, self).__del__()

    @with_reconnect
    def insert_uow(self, process_name, start_timeperiod, end_timeperiod, iteration, job_record):
        """ creates unit_of_work and inserts it into the DB
            @raise DuplicateKeyError if unit_of_work with given parameters already exists """
        start_id = 0
        end_id = iteration

        uow = UnitOfWork()
        uow.timeperiod = start_timeperiod
        uow.start_id = start_id
        uow.end_id = end_id
        uow.start_timeperiod = start_timeperiod
        uow.end_timeperiod = end_timeperiod
        uow.created_at = datetime.utcnow()
        uow.source = None
        uow.sink = None
        uow.state = unit_of_work.STATE_REQUESTED
        uow.process_name = process_name
        uow.number_of_retries = 0
        uow_id = self.uow_dao.insert(uow)

        mq_request = WorkerMqRequest()
        mq_request.process_name = process_name
        mq_request.unit_of_work_id = uow_id

        publisher = self.publishers.get(process_name)
        publisher.publish(mq_request.document)
        publisher.release()

        msg = 'Published: UOW %r for %r in timeperiod %r.' % (uow_id, process_name, start_timeperiod)
        self._log_message(INFO, process_name, job_record, msg)
        return uow

    def _process_state_embryo(self, process_name, job_record, start_timeperiod):
        """ method that takes care of processing job records in STATE_EMBRYO state"""
        time_qualifier = ProcessContext.get_time_qualifier(process_name)
        end_timeperiod = time_helper.increment_timeperiod(time_qualifier, start_timeperiod)

        try:
            uow = self.insert_uow(process_name, start_timeperiod, end_timeperiod, 0, job_record)
        except DuplicateKeyError as e:
            msg = 'Catching up with latest unit_of_work %s in timeperiod %s, because of: %r' \
                  % (process_name, job_record.timeperiod, e)
            self._log_message(WARNING, process_name, job_record, msg)
            uow = self.recover_from_duplicatekeyerror(e)

        if uow is not None:
            self.timetable.update_job_record(process_name, job_record, uow, job.STATE_IN_PROGRESS)
        else:
            msg = 'MANUAL INTERVENTION REQUIRED! Unable to locate unit_of_work for %s in %s' \
                  % (process_name, job_record.timeperiod)
            self._log_message(WARNING, process_name, job_record, msg)

    def _process_state_in_progress(self, process_name, job_record, start_timeperiod):
        """ method that takes care of processing job records in STATE_IN_PROGRESS state"""
        time_qualifier = ProcessContext.get_time_qualifier(process_name)
        end_timeperiod = time_helper.increment_timeperiod(time_qualifier, start_timeperiod)
        actual_timeperiod = time_helper.actual_timeperiod(time_qualifier)
        can_finalize_job_record = self.timetable.can_finalize_job_record(process_name, job_record)
        uow = self.uow_dao.get_one(job_record.related_unit_of_work)
        iteration = int(uow.end_id)

        try:
            if start_timeperiod == actual_timeperiod or can_finalize_job_record is False:
                if uow.state in [unit_of_work.STATE_REQUESTED,
                                 unit_of_work.STATE_IN_PROGRESS,
                                 unit_of_work.STATE_INVALID]:
                    # Large Job processing takes more than 1 tick of Scheduler
                    # Let the Job processing complete - do no updates to Scheduler records
                    pass
                elif uow.state in [unit_of_work.STATE_PROCESSED,
                                   unit_of_work.STATE_CANCELED]:
                    # create new uow to cover new inserts
                    uow = self.insert_uow(process_name, start_timeperiod, end_timeperiod, iteration + 1, job_record)
                    self.timetable.update_job_record(process_name, job_record, uow, job.STATE_IN_PROGRESS)

            elif start_timeperiod < actual_timeperiod and can_finalize_job_record is True:
                if uow.state in [unit_of_work.STATE_REQUESTED,
                                 unit_of_work.STATE_IN_PROGRESS,
                                 unit_of_work.STATE_INVALID]:
                    # Large Job processing has not started yet
                    # Let the Job processing complete - do no updates to Scheduler records
                    pass
                elif uow.state in [unit_of_work.STATE_PROCESSED,
                                   unit_of_work.STATE_CANCELED]:
                    # create new uow for FINAL RUN
                    uow = self.insert_uow(process_name, start_timeperiod, end_timeperiod, iteration + 1, job_record)
                    self.timetable.update_job_record(process_name, job_record, uow, job.STATE_FINAL_RUN)
            else:
                msg = 'Job record %s has timeperiod from future %s vs current time %s' \
                      % (job_record.document['_id'], start_timeperiod, actual_timeperiod)
                self._log_message(ERROR, process_name, job_record, msg)

        except DuplicateKeyError as e:
            uow = self.recover_from_duplicatekeyerror(e)
            if uow is not None:
                self.timetable.update_job_record(process_name, job_record, uow, job_record.state)
            else:
                msg = 'MANUAL INTERVENTION REQUIRED! Unable to identify unit_of_work for %s in %s' \
                      % (process_name, job_record.timeperiod)
                self._log_message(ERROR, process_name, job_record, msg)

    def _process_state_final_run(self, process_name, job_record):
        """method takes care of processing job records in STATE_FINAL_RUN state"""
        uow = self.uow_dao.get_one(job_record.related_unit_of_work)

        if uow.state == unit_of_work.STATE_PROCESSED:
            self.timetable.update_job_record(process_name, job_record, uow, job.STATE_PROCESSED)
            timetable_tree = self.timetable.get_tree(process_name)
            timetable_tree.build_tree()
            msg = 'Transferred job record %s in timeperiod %s to STATE_PROCESSED for %s' \
                  % (job_record.document['_id'], job_record.timeperiod, process_name)
        elif uow.state == unit_of_work.STATE_CANCELED:
            self.timetable.update_job_record(process_name, job_record, uow, job.STATE_SKIPPED)
            msg = 'Transferred job record %s in timeperiod %s to STATE_SKIPPED for %s' \
                  % (job_record.document['_id'], job_record.timeperiod, process_name)
        else:
            msg = 'Suppressed creating uow for %s in timeperiod %s; job record is in %s; uow is in %s' \
                  % (process_name, job_record.timeperiod, job_record.state, uow.state)
        self._log_message(INFO, process_name, job_record, msg)

    def _process_state_skipped(self, process_name, job_record):
        """method takes care of processing job records in STATE_SKIPPED state"""
        msg = 'Skipping job record %s in timeperiod %s. Apparently its most current timeperiod as of %s UTC' \
              % (job_record.document['_id'], job_record.timeperiod, str(datetime.utcnow()))
        self._log_message(WARNING, process_name, job_record, msg)

    def _process_state_processed(self, process_name, job_record):
        """method takes care of processing job records in STATE_PROCESSED state"""
        msg = 'Unexpected state %s of job record %s' % (job_record.state, job_record.document['_id'])
        self._log_message(ERROR, process_name, job_record, msg)
