"""
Tests for setting up an EventsLoader and a BlazeEventsLoader.
"""
from datetime import time
import itertools
from itertools import product

import blaze as bz
import numpy as np
import pandas as pd
from pandas.util.testing import assert_series_equal

from zipline.pipeline import Pipeline, SimplePipelineEngine
from zipline.pipeline.common import (
    EVENT_DATE_FIELD_NAME,
    TS_FIELD_NAME,
    SID_FIELD_NAME,
)
from zipline.pipeline.data import DataSet, Column
from zipline.pipeline.loaders.events import EventsLoader
from zipline.pipeline.loaders.blaze.events import BlazeEventsLoader
from zipline.pipeline.loaders.utils import (
    previous_event_indexer,
    next_event_indexer,
    normalize_timestamp_to_query_time)
from zipline.testing import ZiplineTestCase
from zipline.testing.fixtures import (
    WithAssetFinder,
    WithNYSETradingDays,
)
from zipline.testing.predicates import assert_equal
from zipline.utils.numpy_utils import (
    categorical_dtype,
    datetime64ns_dtype,
    float64_dtype,
    int64_dtype,
)


class EventDataSet(DataSet):

    previous_event_date = Column(dtype=datetime64ns_dtype)
    next_event_date = Column(dtype=datetime64ns_dtype)

    previous_float = Column(dtype=float64_dtype)
    next_float = Column(dtype=float64_dtype)

    previous_datetime = Column(dtype=datetime64ns_dtype)
    next_datetime = Column(dtype=datetime64ns_dtype)

    previous_int = Column(dtype=int64_dtype, missing_value=-1)
    next_int = Column(dtype=int64_dtype, missing_value=-1)

    previous_string = Column(dtype=categorical_dtype, missing_value=None)
    next_string = Column(dtype=categorical_dtype, missing_value=None)

    previous_string_custom_missing = Column(
        dtype=categorical_dtype,
        missing_value=u"<<NULL>>",
    )
    next_string_custom_missing = Column(
        dtype=categorical_dtype,
        missing_value=u"<<NULL>>",
    )


critical_dates = pd.to_datetime([
    '2014-01-05',
    '2014-01-10',
    '2014-01-15',
    '2014-01-20',
])


def make_events_for_sid(sid, event_dates, event_timestamps):
    num_events = len(event_dates)
    return pd.DataFrame({
        'sid': np.full(num_events, sid, dtype=np.int64),
        'timestamp': event_timestamps,
        'event_date': event_dates,
        'float': np.arange(num_events, dtype=np.float64) + sid,
        'int': np.arange(num_events) + sid,
        'datetime': pd.date_range('1990-01-01', periods=num_events).shift(sid),
        'string': ['-'.join([str(sid), str(i)]) for i in range(num_events)],
    })


def make_null_event_date_events(all_sids, timestamp):
    """
    Make an event with a null event_date for all sids.

    Used to test that EventsLoaders filter out null events.
    """
    return pd.DataFrame({
        'sid': all_sids,
        'timestamp': timestamp,
        'event_date': pd.Timestamp('NaT'),
        'float': -9999.0,
        'int': -9999,
        'datetime': pd.Timestamp('1980'),
        'string': 'should be ignored',
    })


def make_events(add_nulls):
    """
    Every event has at least three pieces of data associated with it:

    1. sid : The ID of the asset associated with the event.
    2. event_date : The date on which an event occurred.
    3. timestamp : The date on which we learned about the event.
                   This can be before the occurence_date in the case of an
                   announcement about an upcoming event.

    Events for two different sids shouldn't interact in any way, so the
    interesting cases are determined by the possible interleavings of
    event_date and timestamp for a single sid.

    Fix two events with dates e1, e2 and timestamps t1 and t2.

    Without loss of generality, assume that e1 < e2. (If two events have the
    same occurrence date, the behavior of next/previous event is undefined).

    The remaining possible sequences of events are given by taking all possible
    4-tuples of four ascending dates. For each possible interleaving, we
    generate a set of fake events with those dates and assign them to a new
    sid.
    """
    def gen_date_interleavings():
        for e1, e2, t1, t2 in product(*[critical_dates] * 4):
            if e1 < e2:
                yield (e1, e2, t1, t2)

    event_frames = []
    for sid, (e1, e2, t1, t2) in enumerate(gen_date_interleavings()):
        event_frames.append(make_events_for_sid(sid, [e1, e2], [t1, t2]))

    if add_nulls:
        for date in critical_dates:
            event_frames.append(
                make_null_event_date_events(
                    np.arange(sid + 1),
                    timestamp=date,
                )
            )

    return pd.concat(event_frames, ignore_index=True)


class EventIndexerTestCase(ZiplineTestCase):

    @classmethod
    def init_class_fixtures(cls):
        super(EventIndexerTestCase, cls).init_class_fixtures()
        cls.events = make_events(add_nulls=False).sort('event_date')
        cls.events.reset_index(inplace=True)

    def test_previous_event_indexer(self):
        events = self.events
        event_sids = events['sid'].values
        event_dates = events['event_date'].values
        event_timestamps = events['timestamp'].values

        all_dates = pd.date_range('2014', '2014-01-31')
        all_sids = np.unique(event_sids)

        indexer = previous_event_indexer(
            all_dates,
            all_sids,
            event_dates,
            event_timestamps,
            event_sids,
        )

        # Compute expected results without knowledge of null events.
        for i, sid in enumerate(all_sids):
            self.check_previous_event_indexer(
                events,
                all_dates,
                sid,
                indexer[:, i],
            )

    def check_previous_event_indexer(self,
                                     events,
                                     all_dates,
                                     sid,
                                     indexer):
        relevant_events = events[events.sid == sid]
        self.assertEqual(len(relevant_events), 2)

        ix1, ix2 = relevant_events.index

        # An event becomes a possible value once we're past both its event_date
        # and its timestamp.
        event1_first_eligible = max(
            relevant_events.loc[ix1, ['event_date', 'timestamp']],
        )
        event2_first_eligible = max(
            relevant_events.loc[ix2, ['event_date', 'timestamp']],
        )

        for date, computed_index in zip(all_dates, indexer):
            if date >= event2_first_eligible:
                # If we've seen event 2, it should win even if we've seen event
                # 1, because events are sorted by event_date.
                self.assertEqual(computed_index, ix2)
            elif date >= event1_first_eligible:
                # If we've seen event 1 but not event 2, event 1 should win.
                self.assertEqual(computed_index, ix1)
            else:
                # If we haven't seen either event, then we should have -1 as
                # sentinel.
                self.assertEqual(computed_index, -1)

    def test_next_event_indexer(self):
        events = self.events
        event_sids = events['sid'].values
        event_dates = events['event_date'].values
        event_timestamps = events['timestamp'].values

        all_dates = pd.date_range('2014', '2014-01-31')
        all_sids = np.unique(event_sids)

        indexer = next_event_indexer(
            all_dates,
            all_sids,
            event_dates,
            event_timestamps,
            event_sids,
        )

        # Compute expected results without knowledge of null events.
        for i, sid in enumerate(all_sids):
            self.check_next_event_indexer(
                events,
                all_dates,
                sid,
                indexer[:, i],
            )

    def check_next_event_indexer(self,
                                 events,
                                 all_dates,
                                 sid,
                                 indexer):
        relevant_events = events[events.sid == sid]
        self.assertEqual(len(relevant_events), 2)

        ix1, ix2 = relevant_events.index
        e1, e2 = relevant_events['event_date']
        t1, t2 = relevant_events['timestamp']

        for date, computed_index in zip(all_dates, indexer):
            # An event is eligible to be the next event if it's between the
            # timestamp and the event_date, inclusive.
            if t1 <= date <= e1:
                # If e1 is eligible, it should be chosen even if e2 is
                # eligible, since it's earlier.
                self.assertEqual(computed_index, ix1)
            elif t2 <= date <= e2:
                # e2 is eligible and e1 is not, so e2 should be chosen.
                self.assertEqual(computed_index, ix2)
            else:
                # Neither event is eligible.  Return -1 as a sentinel.
                self.assertEqual(computed_index, -1)


class EventsLoaderTestCase(WithAssetFinder,
                           WithNYSETradingDays,
                           ZiplineTestCase):

    START_DATE = pd.Timestamp('2014-01-01')
    END_DATE = pd.Timestamp('2014-01-30')

    @classmethod
    def init_class_fixtures(cls):
        # This is a rare case where we actually want to do work **before** we
        # call init_class_fixtures.  We choose our sids for WithAssetFinder
        # based on the events generated by make_event_data.
        cls.raw_events = make_events(add_nulls=True)
        cls.raw_events_no_nulls = cls.raw_events[
            cls.raw_events['event_date'].notnull()
        ]
        cls.next_value_columns = {
            EventDataSet.next_datetime: 'datetime',
            EventDataSet.next_event_date: 'event_date',
            EventDataSet.next_float: 'float',
            EventDataSet.next_int: 'int',
            EventDataSet.next_string: 'string',
            EventDataSet.next_string_custom_missing: 'string'
        }
        cls.previous_value_columns = {
            EventDataSet.previous_datetime: 'datetime',
            EventDataSet.previous_event_date: 'event_date',
            EventDataSet.previous_float: 'float',
            EventDataSet.previous_int: 'int',
            EventDataSet.previous_string: 'string',
            EventDataSet.previous_string_custom_missing: 'string'
        }
        cls.loader = cls.make_loader(
            events=cls.raw_events,
            next_value_columns=cls.next_value_columns,
            previous_value_columns=cls.previous_value_columns,
        )
        cls.ASSET_FINDER_EQUITY_SIDS = list(cls.raw_events['sid'].unique())
        cls.ASSET_FINDER_EQUITY_SYMBOLS = [
            's' + str(n) for n in cls.ASSET_FINDER_EQUITY_SIDS
        ]
        super(EventsLoaderTestCase, cls).init_class_fixtures()

    @classmethod
    def make_loader(cls, events, next_value_columns, previous_value_columns):
        # This method exists to be overridden by BlazeEventsLoaderTestCase
        return EventsLoader(events, next_value_columns, previous_value_columns)

    def test_load_with_trading_calendar(self):
        engine = SimplePipelineEngine(
            lambda x: self.loader,
            self.trading_days,
            self.asset_finder,
        )

        results = engine.run_pipeline(
            Pipeline({c.name: c.latest for c in EventDataSet.columns}),
            start_date=self.trading_days[0],
            end_date=self.trading_days[-1],
        )

        for c in EventDataSet.columns:
            if c in self.next_value_columns:
                self.check_next_value_results(c, results[c.name].unstack())
            elif c in self.previous_value_columns:
                self.check_previous_value_results(c, results[c.name].unstack())
            else:
                raise AssertionError("Unexpected column %s." % c)

    def assert_result_contains_all_sids(self, results):
        assert_equal(
            list(map(int, results.columns)),
            self.ASSET_FINDER_EQUITY_SIDS,
        )

    def check_previous_value_results(self, column, results):
        """
        Check previous value results for a single column.
        """
        # Verify that we got a result for every sid.
        self.assert_result_contains_all_sids(results)

        events = self.raw_events_no_nulls
        # Remove timezone info from trading days, since the outputs
        # from pandas won't be tz_localized.
        dates = self.trading_days.tz_localize(None)

        for asset, asset_result in results.iterkv():
            relevant_events = events[events.sid == asset.sid]
            self.assertEqual(len(relevant_events), 2)

            v1, v2 = relevant_events[self.previous_value_columns[column]]
            event1_first_eligible = max(
                # .ix doesn't work here because the frame index contains
                # integers, so 0 is still interpreted as a key.
                relevant_events.iloc[0].loc[['event_date', 'timestamp']],
            )
            event2_first_eligible = max(
                relevant_events.iloc[1].loc[['event_date', 'timestamp']]
            )

            for date, computed_value in zip(dates, asset_result):
                if date >= event2_first_eligible:
                    # If we've seen event 2, it should win even if we've seen
                    # event 1, because events are sorted by event_date.
                    self.assertEqual(computed_value, v2)
                elif date >= event1_first_eligible:
                    # If we've seen event 1 but not event 2, event 1 should
                    # win.
                    self.assertEqual(computed_value, v1)
                else:
                    # If we haven't seen either event, then we should have
                    # column.missing_value.
                    assert_equal(
                        computed_value,
                        column.missing_value,
                        # Coerce from Timestamp to datetime64.
                        allow_datetime_coercions=True,
                    )

    def check_next_value_results(self, column, results):
        """
        Check results for a single column.
        """
        self.assert_result_contains_all_sids(results)

        events = self.raw_events_no_nulls
        # Remove timezone info from trading days, since the outputs
        # from pandas won't be tz_localized.
        dates = self.trading_days.tz_localize(None)
        for asset, asset_result in results.iterkv():
            relevant_events = events[events.sid == asset.sid]
            self.assertEqual(len(relevant_events), 2)

            v1, v2 = relevant_events[self.next_value_columns[column]]
            e1, e2 = relevant_events['event_date']
            t1, t2 = relevant_events['timestamp']

            for date, computed_value in zip(dates, asset_result):
                if t1 <= date <= e1:
                    # If we've seen event 2, it should win even if we've seen
                    # event 1, because events are sorted by event_date.
                    self.assertEqual(computed_value, v1)
                elif t2 <= date <= e2:
                    # If we've seen event 1 but not event 2, event 1 should
                    # win.
                    self.assertEqual(computed_value, v2)
                else:
                    # If we haven't seen either event, then we should have
                    # column.missing_value.
                    assert_equal(
                        computed_value,
                        column.missing_value,
                        # Coerce from Timestamp to datetime64.
                        allow_datetime_coercions=True,
                    )

    def test_wrong_cols(self):
        # Test wrong cols (cols != expected)
        events = pd.DataFrame({
            'c': [5],
            SID_FIELD_NAME: [1],
            TS_FIELD_NAME: [pd.Timestamp('2014')],
            EVENT_DATE_FIELD_NAME: [pd.Timestamp('2014')],
        })

        EventsLoader(events, {EventDataSet.next_float: 'c'}, {})
        EventsLoader(events, {}, {EventDataSet.previous_float: 'c'})

        with self.assertRaises(ValueError) as e:
            EventsLoader(events, {EventDataSet.next_float: 'd'}, {})

        msg = str(e.exception)
        expected = (
            "EventsLoader missing required columns ['d'].\n"
            "Got Columns: ['c', 'event_date', 'sid', 'timestamp']\n"
            "Expected Columns: ['d', 'event_date', 'sid', 'timestamp']"
        )
        self.assertEqual(msg, expected)


class BlazeEventsLoaderTestCase(EventsLoaderTestCase):
    """
    Run the same tests as EventsLoaderTestCase, but using a BlazeEventsLoader.
    """

    @classmethod
    def make_loader(cls, events, next_value_columns, previous_value_columns):
        return BlazeEventsLoader(
            bz.data(events),
            next_value_columns,
            previous_value_columns,
        )


class EventLoaderUtilsTestCase(ZiplineTestCase):
    dates = [pd.Timestamp('2013-01-04 3:00:00'), pd.Timestamp('2013-01-24'),
             pd.Timestamp('2013-04-04'), pd.Timestamp('2013-04-21')]
    combos = list(itertools.permutations(dates))

    def test_normalize_to_query_time(self):
        # Order matters in pandas 18.0.2. Prior to that, using tz_convert on
        # a DatetimeIndex with DST/EST timestamps mixed resulted in some of
        # them being an hour off (1 hour past midnight).
        expected = pd.Series(
            [pd.Timestamp('2013-01-04'),
             pd.Timestamp('2013-01-24'),
             pd.Timestamp('2013-04-04'),
             pd.Timestamp('2013-04-21')]
        ).reset_index(drop=True)
        for combo in self.combos:
            df = pd.DataFrame({"timestamp": combo})
            tz = 'US/Eastern'
            query_time = time(8, 45)
            result = normalize_timestamp_to_query_time(df,
                                                       query_time,
                                                       tz,
                                                       inplace=False,
                                                       ts_field='timestamp')
            result = result.sort("timestamp").reset_index(drop=True)
            assert_series_equal(result['timestamp'], expected)



