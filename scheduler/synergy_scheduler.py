__author__ = 'Bohdan Mushkevych'

from db.model import scheduler_entry
from db.dao.scheduler_entry_dao import SchedulerEntryDao
from datetime import datetime
from threading import Lock
from amqplib.client_0_8 import AMQPException

from mq.flopsy import PublishersPool
from mx.synergy_mx import MX

from system.decorator import with_reconnect
from system.synergy_process import SynergyProcess
from system.repeat_timer import RepeatTimer
from system.process_context import *

from scheduler.dicrete_pipeline import DiscretePipeline
from scheduler.continuous_pipeline import ContinuousPipeline
from scheduler.time_table import TimeTable


class Scheduler(SynergyProcess):
    """ Scheduler encapsulate logic for handling task pipelines """

    def __init__(self, process_name):
        super(Scheduler, self).__init__(process_name)
        self.logger.info('Starting %s' % self.process_name)
        self.publishers = PublishersPool(self.logger)
        self.thread_handlers = dict()
        self.lock = Lock()
        self.timetable = TimeTable(self.logger)
        self.regular_pipeline = ContinuousPipeline(self.logger, self.timetable)
        self.discrete_pipeline = DiscretePipeline(self.logger, self.timetable)
        self.sc_dao = SchedulerEntryDao(self.logger)
        self.mx = None
        self.logger.info('Started %s' % self.process_name)

    def __del__(self):
        for handler in self.thread_handlers:
            handler.cancel()
        self.thread_handlers.clear()
        super(Scheduler, self).__del__()

    def _log_message(self, level, process_name, timetable_record, msg):
        """ method performs logging into log file and TimeTable node"""
        self.timetable.add_log_entry(process_name, timetable_record, datetime.utcnow(), msg)
        self.logger.log(level, msg)

    # **************** Scheduler Methods ************************
    @with_reconnect
    def start(self, *_):
        """ reading scheduler configurations and starting timers to trigger events """
        document_list = self.sc_dao.get_all()

        for document in document_list:
            if document.process_name not in ProcessContext.PROCESS_CONTEXT:
                self.logger.error('Process %r is not known to the system. Skipping it.' % document.process_name)
                continue

            interval = document.interval
            is_active = document.process_state == scheduler_entry.STATE_ON
            process_type = ProcessContext.get_process_type(document.process_name)
            parameters = [document.process_name, document]

            if process_type == TYPE_ALERT:
                function = self.fire_alert
            elif process_type == TYPE_HORIZONTAL_AGGREGATOR:
                function = self.fire_worker
            elif process_type == TYPE_VERTICAL_AGGREGATOR:
                function = self.fire_worker
            elif process_type == TYPE_GARBAGE_COLLECTOR:
                function = self.fire_garbage_collector
            else:
                self.logger.error('Can not start scheduler for %s since it has no processing function' % process_type)
                continue

            handler = RepeatTimer(interval, function, args=parameters)
            self.thread_handlers[document.process_name] = handler

            if is_active:
                handler.start()
                self.logger.info('Started scheduler for %s:%s, triggering every %d seconds'
                                 % (process_type, document.process_name, interval))
            else:
                self.logger.info('Handler for %s:%s registered in Scheduler. Idle until activated.'
                                 % (process_type, document.process_name))

        # as Scheduler is now initialized and running - we can safely start its MX
        self.mx = MX(self)
        self.mx.start_mx_thread()

    def fire_worker(self, *args):
        """requests vertical aggregator (hourly site, daily variant, etc) to start up"""
        try:
            process_name = args[0]
            self.lock.acquire()
            self.logger.info('%s {' % process_name)
            timetable_record = self.timetable.get_next_timetable_record(process_name)
            time_qualifier = ProcessContext.get_time_qualifier(process_name)

            if time_qualifier == ProcessContext.QUALIFIER_HOURLY:
                self.regular_pipeline.manage_pipeline_for_process(process_name, timetable_record)
            else:
                self.discrete_pipeline.manage_pipeline_for_process(process_name, timetable_record)

        except (AMQPException, IOError) as e:
            self.logger.error('AMQPException: %s' % str(e), exc_info=True)
            self.publishers.reset_all(suppress_logging=True)
        except Exception as e:
            self.logger.error('Exception: %s' % str(e), exc_info=True)
        finally:
            self.logger.info('}')
            self.lock.release()

    def fire_alert(self, *args):
        """ Triggers AlertWorker. Makes sure its <dependent on> trees have
            finalized corresponding timeperiods prior to that"""
        try:
            process_name = args[0]
            self.lock.acquire()
            self.logger.info('%s {' % process_name)

            timetable_record = self.timetable.get_next_timetable_record(process_name)
            self.discrete_pipeline.manage_pipeline_with_blocking_dependencies(process_name, timetable_record)
        except (AMQPException, IOError) as e:
            self.logger.error('AMQPException: %s' % str(e), exc_info=True)
            self.publishers.reset_all(suppress_logging=True)
        except Exception as e:
            self.logger.error('Exception: %s' % str(e), exc_info=True)
        finally:
            self.logger.info('}')
            self.lock.release()

    def fire_garbage_collector(self, *args):
        """fires garbage collector to re-run all invalid records"""
        try:
            process_name = args[0]
            self.lock.acquire()
            self.logger.info('%s {' % process_name)

            publisher = self.publishers.get(process_name)
            publisher.publish({})
            publisher.release()

            self.logger.info('Publishing trigger for garbage_collector')
            self.timetable.build_tree()
            self.timetable.validate()
            self.logger.info('Validated Timetable for all trees')
        except (AMQPException, IOError) as e:
            self.logger.error('AMQPException: %s' % str(e), exc_info=True)
            self.publishers.reset_all(suppress_logging=True)
        except Exception as e:
            self.logger.error('fire_garbage_collector: %s' % str(e))
        finally:
            self.logger.info('}')
            self.lock.release()


if __name__ == '__main__':
    from system.process_context import PROCESS_SCHEDULER

    source = Scheduler(PROCESS_SCHEDULER)
    source.start()
