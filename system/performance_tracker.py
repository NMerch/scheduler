__author__ = 'Bohdan Mushkevych'

import time
import os

import psutil

from system.repeat_timer import RepeatTimer
from settings import settings


class FootprintCalculator(object):
    def __init__(self):
        self.pid = os.getpid()

    def group(self, number):
        """ method formats number and inserts thousands separators """
        s = '%d' % number
        groups = []
        while s and s[-1].isdigit():
            groups.append(s[-3:])
            s = s[:-3]
        return s + '\''.join(reversed(groups))

    def get_snapshot_as_list(self):
        ps = psutil.Process(self.pid)
        return (self.group(ps.get_memory_info()[0]),
                self.group(ps.get_memory_info()[1]),
                '%02d' % ps.get_cpu_percent(),
                self.group(psutil.phymem_usage().free),
                self.group(psutil.virtmem_usage().free))

    def get_snapshot(self):
        resp = 'Footprint: RSS=%r VMS=%r CPU=%r; Available: PHYS=%r VIRT=%r' % self.get_snapshot_as_list()
        return resp


class Tracker(object):
    def __init__(self, name):
        self.name = name
        self.per_24h = 0
        self.per_tick = 0

    def increment(self, delta=1):
        self.per_24h += delta
        self.per_tick += delta

    def reset_tick(self):
        self.per_tick = 0

    def reset_24h(self):
        self.per_24h = 0


class TrackerPair(object):
    def __init__(self, name, success='Success', failure='Failure'):
        self.name = name
        self.success = Tracker(failure)
        self.failure = Tracker(success)

    def increment_success(self, delta=1):
        self.failure.increment(delta)

    def increment_failure(self, delta=1):
        self.success.increment(delta)

    def reset_tick(self):
        self.success.reset_tick()
        self.failure.reset_tick()

    def reset_24h(self):
        self.success.reset_24h()
        self.failure.reset_24h()

    def to_string(self, tick_interval_seconds, show_header=True):
        header = self.name + ' : ' + self.failure.name + '/' + self.success.name + '.' if show_header else ''
        return header + 'In last {0:d} seconds: {1:d}/{2:d}. In last 24 hours: {3:d}/{4:d}'.format(
            tick_interval_seconds,
            self.failure.per_tick,
            self.success.per_tick,
            self.failure.per_24h,
            self.success.per_24h)


class TickerThread(object):
    SECONDS_IN_24_HOURS = 86400
    TICKS_BETWEEN_FOOTPRINTS = 10

    def __init__(self, logger):
        self.logger = logger
        self.trackers = dict()
        self.interval = settings['perf_ticker_interval']
        self.mark_24_hours = time.time()
        self.mark_footprint = time.time()
        self.footprint = FootprintCalculator()
        self.timer = RepeatTimer(self.interval, self._run_tick_thread)

    def add_tracker(self, tracker):
        self.trackers[tracker.name] = tracker

    def get_tracker(self, name):
        return self.trackers[name]

    def start(self):
        self.timer.start()

    def cancel(self):
        self.timer.cancel()

    def is_alive(self):
        return self.timer.is_alive()

    def _print_footprint(self):
        if time.time() - self.mark_footprint > self.TICKS_BETWEEN_FOOTPRINTS * self.interval:
            self.logger.info(self.footprint.get_snapshot())
            self.mark_footprint = time.time()

    def _run_tick_thread(self):
        self._print_footprint()

        summary_output = ''
        for tracker_name, tracker in self.trackers.iteritems():
            summary_output += tracker.to_string(self.interval) + '\n'
        self.logger.info(summary_output)

        for tracker_name, tracker in self.trackers.iteritems():
            tracker.reset_tick()
            if time.time() - self.mark_24_hours > self.SECONDS_IN_24_HOURS:
                tracker.reset_24h()
                self.mark_24_hours = time.time()


class SimpleTracker(TickerThread):
    TRACKER_PERFORMANCE = 'Performance'

    def __init__(self, logger):
        super(SimpleTracker, self).__init__(logger)
        self.add_tracker(TrackerPair(self.TRACKER_PERFORMANCE))

    @property
    def tracker(self):
        return self.get_tracker(self.TRACKER_PERFORMANCE)


class SessionPerformanceTracker(TickerThread):
    TRACKER_INSERT = 'Insert'
    TRACKER_UPDATE = 'Update'

    def __init__(self, logger):
        super(SessionPerformanceTracker, self).__init__(logger)
        self.add_tracker(TrackerPair(self.TRACKER_INSERT))
        self.add_tracker(TrackerPair(self.TRACKER_UPDATE))

    @property
    def insert(self):
        return self.get_tracker(self.TRACKER_INSERT)

    @property
    def update(self):
        return self.get_tracker(self.TRACKER_UPDATE)


class AggregatorPerformanceTicker(SimpleTracker):
    STATE_IDLE = 'state_idle'
    STATE_PROCESSING = 'state_processing'

    def __init__(self, logger):
        super(AggregatorPerformanceTicker, self).__init__(logger)
        self.state = self.STATE_IDLE
        self.per_job = 0
        self.uow_obj = None
        self.state_triggered_at = time.time()

    def _run_tick_thread(self):
        super(AggregatorPerformanceTicker, self)._run_tick_thread()

        if self.state == self.STATE_PROCESSING:
            msg = 'State: %s for %d sec; %d in this uow;' \
                  % (self.state,
                     time.time() - self.state_triggered_at,
                     self.per_job)
        else:
            msg = 'State: %s for %d sec;' % (self.state, time.time() - self.state_triggered_at)
        self.logger.info(msg)

    def increment(self):
        self.tracker.increment_success()
        self.per_job += 1

    def start_uow(self, uow_obj):
        self.state = self.STATE_PROCESSING
        self.uow_obj = uow_obj
        self.state_triggered_at = time.time()

    def finish_uow(self):
        _id = self.uow_obj.document['_id']
        self.logger.info('Success: unit_of_work %s in timeperiod %s; processed %d entries in %d seconds'
                         % (_id,
                            self.uow_obj.timeperiod,
                            self.per_job,
                            time.time() - self.state_triggered_at))
        self.cancel_uow()

    def cancel_uow(self):
        self.state = self.STATE_IDLE
        self.uow_obj = None
        self.state_triggered_at = time.time()
        self.per_job = 0
