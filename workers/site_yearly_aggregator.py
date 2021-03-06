__author__ = 'Bohdan Mushkevych'

from workers.site_daily_aggregator import SiteDailyAggregator
from system import time_helper


class SiteYearlyAggregator(SiteDailyAggregator):
    """ class works as an aggregator from the monthly_site_collection into the yearly_site_collection """

    def __init__(self, process_name):
        super(SiteYearlyAggregator, self).__init__(process_name)

    def _init_sink_key(self, *args):
        return args[0], time_helper.month_to_year(args[1])


if __name__ == '__main__':
    from constants import PROCESS_SITE_YEARLY

    source = SiteYearlyAggregator(PROCESS_SITE_YEARLY)
    source.start()
