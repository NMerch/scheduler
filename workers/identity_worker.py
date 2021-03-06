""" Module contains logic for YES worker - one that marks any units_of_work as complete """

__author__ = 'Bohdan Mushkevych'

from datetime import datetime
from db.model import unit_of_work
from db.model.worker_mq_request import WorkerMqRequest
from db.dao.unit_of_work_dao import UnitOfWorkDao
from workers.abstract_mq_worker import AbstractMqWorker
from system.performance_tracker import AggregatorPerformanceTicker


class IdentityWorker(AbstractMqWorker):
    """ Marks all unit_of_work as <complete>"""

    def __init__(self, process_name):
        super(IdentityWorker, self).__init__(process_name)
        self.hadoop_process = None
        self.uow_dao = UnitOfWorkDao(self.logger)

    def __del__(self):
        super(IdentityWorker, self).__del__()

    # **************** Abstract Methods ************************
    def _init_performance_ticker(self, logger):
        self.performance_ticker = AggregatorPerformanceTicker(logger)
        self.performance_ticker.start()

    # ********************** thread-related methods ****************************
    def _mq_callback(self, message):
        """ try/except wrapper
        in case exception breaks the abstract method, this method:
        - catches the exception
        - logs the exception
        - marks unit of work as INVALID"""
        try:
            mq_request = WorkerMqRequest(message.body)
            uow = self.uow_dao.get_one(mq_request.unit_of_work_id)
            if uow.state in [unit_of_work.STATE_CANCELED, unit_of_work.STATE_PROCESSED]:
                # garbage collector might have reposted this UOW
                self.logger.warning('Skipping unit_of_work: id %s; state %s;'
                                    % (str(message.body), uow.state), exc_info=False)
                self.consumer.acknowledge(message.delivery_tag)
                return
        except Exception:
            self.logger.error('Safety fuse. Can not identify unit_of_work %s' % str(message.body), exc_info=True)
            self.consumer.acknowledge(message.delivery_tag)
            return

        try:
            self.performance_ticker.start_uow(uow)
            uow.state = unit_of_work.STATE_PROCESSED
            uow.number_of_processed_documents = 0
            uow.started_at = datetime.utcnow()
            uow.finished_at = datetime.utcnow()
            self.uow_dao.update(uow)
            self.performance_ticker.finish_uow()
        except Exception as e:
            uow.state = unit_of_work.STATE_INVALID
            self.uow_dao.update(uow)
            self.performance_ticker.cancel_uow()
            self.logger.error('Safety fuse while processing unit_of_work %s in timeperiod %s : %r'
                              % (message.body, uow.timeperiod, e), exc_info=True)
        finally:
            self.consumer.acknowledge(message.delivery_tag)
            self.consumer.close()
