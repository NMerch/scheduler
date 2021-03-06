__author__ = 'Bohdan Mushkevych'

from db.model import job
from db.dao.unit_of_work_dao import UnitOfWorkDao
from db.dao.job_dao import JobDao
from datetime import datetime
from logging import INFO, WARNING, ERROR

from mq.flopsy import PublishersPool
from system.decorator import with_reconnect


class AbstractPipeline(object):
    """ Abstract state machine used to govern all processes and their states """

    def __init__(self, logger, timetable, name):
        self.name = name
        self.logger = logger
        self.publishers = PublishersPool(self.logger)
        self.timetable = timetable
        self.uow_dao = UnitOfWorkDao(self.logger)
        self.job_dao = JobDao(self.logger)

    def __del__(self):
        try:
            self.logger.info('Closing Flopsy Publishers Pool...')
            self.publishers.close()
        except Exception as e:
            self.logger.error('Exception caught while closing Flopsy Publishers Pool: %s' % str(e))

    def _log_message(self, level, process_name, job_record, msg):
        """ method performs logging into log file and Timetable's tree node"""
        self.timetable.add_log_entry(process_name, job_record, datetime.utcnow(), msg)
        self.logger.log(level, msg)

    @with_reconnect
    def recover_from_duplicatekeyerror(self, e):
        """ try to recover from DuplicateKeyError """
        start_id = None
        end_id = None
        process_name = None
        timeperiod = None

        if hasattr(e, 'start_id'):
            start_id = e.start_id
        if hasattr(e, 'end_id'):
            end_id = e.end_id
        if hasattr(e, 'process_name'):
            process_name = e.process_name
        if hasattr(e, 'timeperiod'):
            timeperiod = e.timeperiod

        if process_name is not None \
                and timeperiod is not None \
                and start_id is not None \
                and end_id is not None:
            try:
                return self.uow_dao.get_by_params(process_name, timeperiod, start_id, end_id)
            except LookupError as e:
                self.logger.error('Unable to recover from DB error due to %s' % e.message, exc_info=True)
        else:
            msg = 'Unable to locate unit_of_work due to incomplete primary key ' \
                  '(process_name=%s, timeperiod=%s, start_id=%s, end_id=%s)' \
                  % (process_name, timeperiod, start_id, end_id)
            self.logger.error(msg)

    def _process_state_embryo(self, process_name, job_record, start_timeperiod):
        """ method that takes care of processing job records in STATE_EMBRYO state"""
        pass

    def _process_state_in_progress(self, process_name, job_record, start_timeperiod):
        """ method that takes care of processing job records in STATE_IN_PROGRESS state"""
        pass

    def _process_state_final_run(self, process_name, job_record):
        """method takes care of processing job records in STATE_FINAL_RUN state"""
        pass

    def _process_state_skipped(self, process_name, job_record):
        """method takes care of processing job records in STATE_FINAL_SKIPPED state"""
        pass

    def _process_state_processed(self, process_name, job_record):
        """method takes care of processing job records in STATE_FINAL_SKIPPED state"""
        pass

    def manage_pipeline_with_blocking_children(self, process_name, job_record):
        """ method will trigger job processing only if all children are in STATE_PROCESSED or STATE_SKIPPED
         and if all external dependencies are finalized (i.e. in STATE_PROCESSED or STATE_SKIPPED) """
        green_light = self.timetable.can_finalize_job_record(process_name, job_record)
        if green_light:
            self.manage_pipeline_for_process(process_name, job_record)
        else:
            msg = '%s for timeperiod %r is blocked by unprocessed children/dependencies. Waiting another tick' \
                  % (process_name, job_record.timeperiod)
            self._log_message(INFO, process_name, job_record, msg)

    def manage_pipeline_with_blocking_dependencies(self, process_name, job_record):
        """ method will trigger job processing only if _all_ dependencies are in STATE_PROCESSED
         method will transfer current job into STATE_SKIPPED if any dependency is in STATE_SKIPPED """
        all_finalized, all_processed, skipped_present = self.timetable.is_dependent_on_finalized(process_name,
                                                                                                 job_record)
        if all_processed:
            self.manage_pipeline_for_process(process_name, job_record)
        elif skipped_present:
            # As soon as among <dependent on> periods are in STATE_SKIPPED
            # there is very little sense in waiting for them to become STATE_PROCESSED
            # Skip this timeperiod itself
            job_record.state = job.STATE_SKIPPED
            self.job_dao.update(job_record)
            tree = self.timetable.get_tree(process_name)
            tree.update_node_by_process(process_name, job_record)

            msg = '%s for timeperiod %r is blocked by STATE_SKIPPED dependencies. ' \
                  'Transferred the job to STATE_SKIPPED' % (process_name, job_record.timeperiod)
            self._log_message(WARNING, process_name, job_record, msg)
        else:
            msg = '%s for timeperiod %r is blocked by unprocessed dependencies. Waiting another tick' \
                  % (process_name, job_record.timeperiod)
            self._log_message(INFO, process_name, job_record, msg)

    def manage_pipeline_for_process(self, process_name, job_record):
        """ method main duty - is to _avoid_ publishing another unit_of_work, if previous was not yet processed
        In case the Scheduler sees that the unit_of_work is pending it could either update boundaries of the processing
        or wait another tick """
        try:
            if job_record.state == job.STATE_EMBRYO:
                self._process_state_embryo(process_name, job_record, job_record.timeperiod)

            elif job_record.state == job.STATE_IN_PROGRESS:
                self._process_state_in_progress(process_name, job_record, job_record.timeperiod)

            elif job_record.state == job.STATE_FINAL_RUN:
                self._process_state_final_run(process_name, job_record)

            elif job_record.state == job.STATE_SKIPPED:
                self._process_state_skipped(process_name, job_record)

            elif job_record.state == job.STATE_PROCESSED:
                self._process_state_processed(process_name, job_record)

            else:
                msg = 'Unknown state %s of the job %s' % (job_record.state, job_record.document['_id'])
                self._log_message(ERROR, process_name, job_record, msg)

        except LookupError as e:
            self.timetable.failed_on_processing_job_record(process_name, job_record.timeperiod)
            msg = 'Increasing fail counter for %s in timeperiod %s, because of: %r' \
                  % (process_name, job_record.timeperiod, e)
            self._log_message(WARNING, process_name, job_record, msg)
