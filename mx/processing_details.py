"""
Created on 2011-10-21

@author: Bohdan Mushkevych
"""
from threading import RLock
from data_collections.abstract_collection import AbstractCollection
from system.collection_context import CollectionContext, COLLECTION_TIMETABLE_YEARLY, \
    COLLECTION_TIMETABLE_MONTHLY, COLLECTION_TIMETABLE_DAILY, COLLECTION_TIMETABLE_HOURLY

try:
    from scheduler.time_table import thread_safe
    from scheduler.time_table_collection import TimeTableCollection
except:
    from time_table import thread_safe
    from time_table_collection import TimeTableCollection



class ProcessingDetails(object):
    """ Reads from MongoDB status of timeperiods and their processing status """

    def __init__(self, logger):
        self.lock = RLock()
        self.logger = logger

    @thread_safe
    def retrieve_for_timestamp(self, timestamp, unprocessed_only):
        """ method iterates thru all objects in timetable collections and load them into timetable"""
        resp = dict()
        resp.update(self._search_by_level(CollectionContext.get_collection(self.logger, COLLECTION_TIMETABLE_HOURLY),
                                          timestamp, unprocessed_only))
        resp.update(self._search_by_level(CollectionContext.get_collection(self.logger, COLLECTION_TIMETABLE_DAILY),
                                          timestamp, unprocessed_only))
        resp.update(self._search_by_level(CollectionContext.get_collection(self.logger, COLLECTION_TIMETABLE_MONTHLY),
                                          timestamp, unprocessed_only))
        resp.update(self._search_by_level(CollectionContext.get_collection(self.logger, COLLECTION_TIMETABLE_YEARLY),
                                          timestamp, unprocessed_only))
        return resp

    @thread_safe
    def _search_by_level(self, collection, timestamp, unprocessed_only):
        """ method iterated thru all documents in all timetable collections and builds tree of known system state"""
        resp = dict()
        try:
            if unprocessed_only:
                query = { AbstractCollection.TIMESTAMP : {'$regex': timestamp },
                          TimeTableCollection.STATE : {'$ne' : TimeTableCollection.STATE_PROCESSED }}
            else:
                query = { AbstractCollection.TIMESTAMP : {'$regex': timestamp }}

            cursor = collection.find(query)
            if cursor.count() == 0:
                self.logger.warning('No TimeTable Records in %s.' % str(collection))
            else:
                for document in cursor:
                    obj = TimeTableCollection(document)
                    key = (obj.get_process_name(), obj.get_timestamp())
                    resp[key] = obj
                    print(key)
        except Exception as e:
            self.logger.error('ProcessingDetails error: %s' % str(e))
        return resp

    
if __name__ == '__main__':
    import logging
    pd = ProcessingDetails(logging)
    resp = pd.retrieve_for_timestamp('201110', false)
    print('%r' % resp)
