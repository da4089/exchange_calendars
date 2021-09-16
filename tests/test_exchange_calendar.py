#
# Copyright 2018 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations
from datetime import time
from os.path import abspath, dirname, join
from unittest import TestCase
import typing
import re
import functools
from itertools import islice
import pathlib
from collections import abc

import pytest
import numpy as np
import pandas as pd
import pandas.testing as tm
from pandas import Timedelta, read_csv
from parameterized import parameterized
from pytz import UTC, timezone
from toolz import concat

from exchange_calendars import get_calendar
from exchange_calendars.calendar_utils import (
    ExchangeCalendarDispatcher,
    _default_calendar_aliases,
    _default_calendar_factories,
)
from exchange_calendars.errors import (
    CalendarNameCollision,
    InvalidCalendarName,
    NoSessionsError,
)
from exchange_calendars.exchange_calendar import ExchangeCalendar, days_at_time
from .test_utils import T


class FakeCalendar(ExchangeCalendar):
    name = "DMY"
    tz = "Asia/Ulaanbaatar"
    open_times = ((None, time(11, 13)),)
    close_times = ((None, time(11, 49)),)


class CalendarRegistrationTestCase(TestCase):
    def setup_method(self, method):
        self.dummy_cal_type = FakeCalendar
        self.dispatcher = ExchangeCalendarDispatcher({}, {}, {})

    def teardown_method(self, method):
        self.dispatcher.clear_calendars()

    def test_register_calendar(self):
        # Build a fake calendar
        dummy_cal = self.dummy_cal_type()

        # Try to register and retrieve the calendar
        self.dispatcher.register_calendar("DMY", dummy_cal)
        retr_cal = self.dispatcher.get_calendar("DMY")
        self.assertEqual(dummy_cal, retr_cal)

        # Try to register again, expecting a name collision
        with self.assertRaises(CalendarNameCollision):
            self.dispatcher.register_calendar("DMY", dummy_cal)

        # Deregister the calendar and ensure that it is removed
        self.dispatcher.deregister_calendar("DMY")
        with self.assertRaises(InvalidCalendarName):
            self.dispatcher.get_calendar("DMY")

    def test_register_calendar_type(self):
        self.dispatcher.register_calendar_type("DMY", self.dummy_cal_type)
        retr_cal = self.dispatcher.get_calendar("DMY")
        self.assertEqual(self.dummy_cal_type, type(retr_cal))

    def test_both_places_are_checked(self):
        dummy_cal = self.dummy_cal_type()

        # if instance is registered, can't register type with same name
        self.dispatcher.register_calendar("DMY", dummy_cal)
        with self.assertRaises(CalendarNameCollision):
            self.dispatcher.register_calendar_type("DMY", type(dummy_cal))

        self.dispatcher.deregister_calendar("DMY")

        # if type is registered, can't register instance with same name
        self.dispatcher.register_calendar_type("DMY", type(dummy_cal))

        with self.assertRaises(CalendarNameCollision):
            self.dispatcher.register_calendar("DMY", dummy_cal)

    def test_force_registration(self):
        self.dispatcher.register_calendar("DMY", self.dummy_cal_type())
        first_dummy = self.dispatcher.get_calendar("DMY")

        # force-register a new instance
        self.dispatcher.register_calendar("DMY", self.dummy_cal_type(), force=True)

        second_dummy = self.dispatcher.get_calendar("DMY")

        self.assertNotEqual(first_dummy, second_dummy)


class DefaultsTestCase(TestCase):
    def test_default_calendars(self):
        dispatcher = ExchangeCalendarDispatcher(
            calendars={},
            calendar_factories=_default_calendar_factories,
            aliases=_default_calendar_aliases,
        )

        # These are ordered aliases first, so that we can deregister the
        # canonical factories when we're done with them, and we'll be done with
        # them after they've been used by all aliases and by canonical name.
        for name in concat([_default_calendar_aliases, _default_calendar_factories]):
            self.assertIsNotNone(
                dispatcher.get_calendar(name), "get_calendar(%r) returned None" % name
            )
            dispatcher.deregister_calendar(name)


class DaysAtTimeTestCase(TestCase):
    @parameterized.expand(
        [
            # NYSE standard day
            (
                "2016-07-19",
                0,
                time(9, 31),
                timezone("America/New_York"),
                "2016-07-19 9:31",
            ),
            # CME standard day
            (
                "2016-07-19",
                -1,
                time(17, 1),
                timezone("America/Chicago"),
                "2016-07-18 17:01",
            ),
            # CME day after DST start
            (
                "2004-04-05",
                -1,
                time(17, 1),
                timezone("America/Chicago"),
                "2004-04-04 17:01",
            ),
            # ICE day after DST start
            (
                "1990-04-02",
                -1,
                time(19, 1),
                timezone("America/Chicago"),
                "1990-04-01 19:01",
            ),
        ]
    )
    def test_days_at_time(self, day, day_offset, time_offset, tz, expected):
        days = pd.DatetimeIndex([pd.Timestamp(day, tz=tz)])
        result = days_at_time(days, time_offset, tz, day_offset)[0]
        expected = pd.Timestamp(expected, tz=tz).tz_convert(UTC)
        self.assertEqual(result, expected)


class ExchangeCalendarTestBase(object):

    # Override in subclasses.
    answer_key_filename = None
    calendar_class = None

    # Affects test_start_bound. Should be set to earliest date for which
    # calendar can be instantiated, or None if no start bound.
    START_BOUND: pd.Timestamp | None = None
    # Affects test_end_bound. Should be set to latest date for which
    # calendar can be instantiated, or None if no end bound.
    END_BOUND: pd.Timestamp | None = None

    # Affects tests that care about the empty periods between sessions. Should
    # be set to False for 24/7 calendars.
    GAPS_BETWEEN_SESSIONS = True

    # Affects tests that care about early closes. Should be set to False for
    # calendars that don't have any early closes.
    HAVE_EARLY_CLOSES = True

    # Affects tests that care about late opens. Since most do not, defaulting
    # to False.
    HAVE_LATE_OPENS = False

    # Affects test_for_breaks. True if one or more calendar sessions has a
    # break.
    HAVE_BREAKS = False

    # Affects test_session_has_break.
    SESSION_WITH_BREAK = None  # None if no session has a break
    SESSION_WITHOUT_BREAK = T("2011-06-15")  # None if all sessions have breaks

    # Affects test_sanity_check_session_lengths. Should be set to the largest
    # number of hours that ever appear in a single session.
    MAX_SESSION_HOURS = 0

    # Affects test_minute_index_to_session_labels.
    # Change these if the start/end dates of your test suite don't contain the
    # defaults.
    MINUTE_INDEX_TO_SESSION_LABELS_START = pd.Timestamp("2011-01-04", tz=UTC)
    MINUTE_INDEX_TO_SESSION_LABELS_END = pd.Timestamp("2011-04-04", tz=UTC)

    # Affects tests around daylight savings. If possible, should contain two
    # dates that are not both in the same daylight savings regime.
    DAYLIGHT_SAVINGS_DATES = ["2004-04-05", "2004-11-01"]

    # Affects test_start_end. Change these if your calendar start/end
    # dates between 2010-01-03 and 2010-01-10 don't match the defaults.
    TEST_START_END_FIRST = pd.Timestamp("2010-01-03", tz=UTC)
    TEST_START_END_LAST = pd.Timestamp("2010-01-10", tz=UTC)
    TEST_START_END_EXPECTED_FIRST = pd.Timestamp("2010-01-04", tz=UTC)
    TEST_START_END_EXPECTED_LAST = pd.Timestamp("2010-01-08", tz=UTC)

    @staticmethod
    def load_answer_key(filename):
        """
        Load a CSV from tests/resources/{filename}.csv
        """
        fullpath = join(
            dirname(abspath(__file__)),
            "./resources",
            filename + ".csv",
        )

        return read_csv(
            fullpath,
            index_col=0,
            # NOTE: Merely passing parse_dates=True doesn't cause pandas to set
            # the dtype correctly, and passing all reasonable inputs to the
            # dtype kwarg cause read_csv to barf.
            parse_dates=[0, 1, 2],
            date_parser=lambda x: pd.Timestamp(x, tz=UTC),
        )

    @classmethod
    def setup_class(cls):
        cls.answers = cls.load_answer_key(cls.answer_key_filename)

        cls.start_date = cls.answers.index[0]
        cls.end_date = cls.answers.index[-1]
        cls.calendar = cls.calendar_class(cls.start_date, cls.end_date)

        cls.one_minute = pd.Timedelta(1, "T")
        cls.one_hour = pd.Timedelta(1, "H")
        cls.one_day = pd.Timedelta(1, "D")
        cls.today = pd.Timestamp.now(tz="UTC").floor("D")

    @classmethod
    def teardown_class(cls):
        cls.calendar = None
        cls.answers = None

    def test_bound_start(self):
        if self.START_BOUND is not None:
            cal = self.calendar_class(self.START_BOUND, self.today)
            self.assertIsInstance(cal, ExchangeCalendar)
            start = self.START_BOUND - pd.DateOffset(days=1)
            with pytest.raises(ValueError, match=re.escape(f"{start}")):
                self.calendar_class(start, self.today)
        else:
            # verify no bound imposed
            cal = self.calendar_class(pd.Timestamp("1902-01-01", tz="UTC"), self.today)
            self.assertIsInstance(cal, ExchangeCalendar)

    def test_bound_end(self):
        if self.END_BOUND is not None:
            cal = self.calendar_class(self.today, self.END_BOUND)
            self.assertIsInstance(cal, ExchangeCalendar)
            end = self.END_BOUND + pd.DateOffset(days=1)
            with pytest.raises(ValueError, match=re.escape(f"{end}")):
                self.calendar_class(self.today, end)
        else:
            # verify no bound imposed
            cal = self.calendar_class(self.today, pd.Timestamp("2050-01-01", tz="UTC"))
            self.assertIsInstance(cal, ExchangeCalendar)

    def test_sanity_check_session_lengths(self):
        # make sure that no session is longer than self.MAX_SESSION_HOURS hours
        for session in self.calendar.all_sessions:
            o, c = self.calendar.open_and_close_for_session(session)
            delta = c - o
            self.assertLessEqual(delta.seconds / 3600, self.MAX_SESSION_HOURS)

    def test_calculated_against_csv(self):
        tm.assert_index_equal(self.calendar.schedule.index, self.answers.index)

    def test_adhoc_holidays_specification(self):
        """adhoc holidays should be tz-naive (#33, #39)."""
        dti = pd.DatetimeIndex(self.calendar.adhoc_holidays)
        assert dti.tz is None

    def test_is_open_on_minute(self):
        one_minute = pd.Timedelta(minutes=1)
        m = self.calendar.is_open_on_minute

        for market_minute in self.answers.market_open[1:]:
            market_minute_utc = market_minute
            # The exchange should be classified as open on its first minute
            self.assertTrue(m(market_minute_utc, _parse=False))

            if self.GAPS_BETWEEN_SESSIONS:
                # Decrement minute by one, to minute where the market was not
                # open
                pre_market = market_minute_utc - one_minute
                self.assertFalse(m(pre_market, _parse=False))

        for market_minute in self.answers.market_close[:-1]:
            close_minute_utc = market_minute
            # should be open on its last minute
            self.assertTrue(m(close_minute_utc, _parse=False))

            if self.GAPS_BETWEEN_SESSIONS:
                # increment minute by one minute, should be closed
                post_market = close_minute_utc + one_minute
                self.assertFalse(m(post_market, _parse=False))

    def _verify_minute(
        self,
        calendar,
        minute,
        next_open_answer,
        prev_open_answer,
        next_close_answer,
        prev_close_answer,
    ):
        next_open = calendar.next_open(minute, _parse=False)
        self.assertEqual(next_open, next_open_answer)

        prev_open = self.calendar.previous_open(minute, _parse=False)
        self.assertEqual(prev_open, prev_open_answer)

        next_close = self.calendar.next_close(minute, _parse=False)
        self.assertEqual(next_close, next_close_answer)

        prev_close = self.calendar.previous_close(minute, _parse=False)
        self.assertEqual(prev_close, prev_close_answer)

    def test_next_prev_open_close(self):
        # for each session, check:
        # - the minute before the open (if gaps exist between sessions)
        # - the first minute of the session
        # - the second minute of the session
        # - the minute before the close
        # - the last minute of the session
        # - the first minute after the close (if gaps exist between sessions)
        opens = self.answers.market_open.iloc[1:-2]
        closes = self.answers.market_close.iloc[1:-2]

        previous_opens = self.answers.market_open.iloc[:-1]
        previous_closes = self.answers.market_close.iloc[:-1]

        next_opens = self.answers.market_open.iloc[2:]
        next_closes = self.answers.market_close.iloc[2:]

        for (
            open_minute,
            close_minute,
            previous_open,
            previous_close,
            next_open,
            next_close,
        ) in zip(
            opens, closes, previous_opens, previous_closes, next_opens, next_closes
        ):

            minute_before_open = open_minute - self.one_minute

            # minute before open
            if self.GAPS_BETWEEN_SESSIONS:
                self._verify_minute(
                    self.calendar,
                    minute_before_open,
                    open_minute,
                    previous_open,
                    close_minute,
                    previous_close,
                )

            # open minute
            self._verify_minute(
                self.calendar,
                open_minute,
                next_open,
                previous_open,
                close_minute,
                previous_close,
            )

            # second minute of session
            self._verify_minute(
                self.calendar,
                open_minute + self.one_minute,
                next_open,
                open_minute,
                close_minute,
                previous_close,
            )

            # minute before the close
            self._verify_minute(
                self.calendar,
                close_minute - self.one_minute,
                next_open,
                open_minute,
                close_minute,
                previous_close,
            )

            # the close
            self._verify_minute(
                self.calendar,
                close_minute,
                next_open,
                open_minute,
                next_close,
                previous_close,
            )

            # minute after the close
            if self.GAPS_BETWEEN_SESSIONS:
                self._verify_minute(
                    self.calendar,
                    close_minute + self.one_minute,
                    next_open,
                    open_minute,
                    next_close,
                    close_minute,
                )

    def test_next_prev_minute(self):
        all_minutes = self.calendar.all_minutes

        # test 20,000 minutes because it takes too long to do the rest.
        for idx, minute in enumerate(all_minutes[1:20000]):
            self.assertEqual(
                all_minutes[idx + 2], self.calendar.next_minute(minute, _parse=False)
            )

            self.assertEqual(
                all_minutes[idx], self.calendar.previous_minute(minute, _parse=False)
            )

        # test a couple of non-market minutes
        if self.GAPS_BETWEEN_SESSIONS:
            for open_minute in self.answers.market_open[1:]:
                hour_before_open = open_minute - self.one_hour
                self.assertEqual(
                    open_minute,
                    self.calendar.next_minute(hour_before_open, _parse=False),
                )

            for close_minute in self.answers.market_close[1:]:
                hour_after_close = close_minute + self.one_hour
                self.assertEqual(
                    close_minute,
                    self.calendar.previous_minute(hour_after_close, _parse=False),
                )

    def test_date_to_session_label(self):
        m = self.calendar.date_to_session_label
        sessions = self.answers.index[:30]  # first 30 sessions

        # test for error if request session prior to first calendar session.
        date = self.answers.index[0] - self.one_day
        error_msg = (
            "Cannot get a session label prior to the first calendar"
            f" session ('{self.answers.index[0]}'). Consider passing"
            " `direction` as 'next'."
        )
        with pytest.raises(ValueError, match=re.escape(error_msg)):
            m(date, "previous", _parse=False)

        # direction as "previous"
        dates = pd.date_range(sessions[0], sessions[-1], freq="D")
        last_session = None
        for date in dates:
            session_label = m(date, "previous", _parse=False)
            if date in sessions:
                assert session_label == date
                last_session = session_label
            else:
                assert session_label == last_session

        # direction as "next"
        last_session = None
        for date in dates.sort_values(ascending=False):
            session_label = m(date, "next", _parse=False)
            if date in sessions:
                assert session_label == date
                last_session = session_label
            else:
                assert session_label == last_session

        # test for error if request session after last calendar session.
        date = self.answers.index[-1] + self.one_day
        error_msg = (
            "Cannot get a session label later than the last calendar"
            f" session ('{self.answers.index[-1]}'). Consider passing"
            " `direction` as 'previous'."
        )
        with pytest.raises(ValueError, match=re.escape(error_msg)):
            m(date, "next", _parse=False)

        if self.GAPS_BETWEEN_SESSIONS:
            not_sessions = dates[~dates.isin(sessions)][:5]
            for not_session in not_sessions:
                error_msg = (
                    f"`date` '{not_session}' is not a session label. Consider"
                    " passing a `direction`."
                )
                with pytest.raises(ValueError, match=re.escape(error_msg)):
                    m(not_session, "none", _parse=False)
                # test default behaviour
                with pytest.raises(ValueError, match=re.escape(error_msg)):
                    m(not_session, _parse=False)

            # non-valid direction (can only be thrown if gaps between sessions)
            error_msg = (
                "'not a direction' is not a valid `direction`. Valid `direction`"
                ' values are "next", "previous" and "none".'
            )
            with pytest.raises(ValueError, match=re.escape(error_msg)):
                m(not_session, "not a direction", _parse=False)

    def test_minute_to_session_label(self):
        m = self.calendar.minute_to_session_label
        # minute is prior to first session's open
        minute_before_first_open = self.answers.iloc[0].market_open - self.one_minute
        session_label = self.answers.index[0]
        minutes_that_resolve_to_this_session = [
            m(minute_before_first_open, _parse=False),
            m(minute_before_first_open, direction="next", _parse=False),
        ]

        unique_session_labels = set(minutes_that_resolve_to_this_session)
        self.assertTrue(len(unique_session_labels) == 1)
        self.assertIn(session_label, unique_session_labels)

        with self.assertRaises(ValueError):
            m(minute_before_first_open, direction="previous", _parse=False)
        with self.assertRaises(ValueError):
            m(minute_before_first_open, direction="none", _parse=False)

        # minute is between first session's open and last session's close
        for idx, (session_label, open_minute, close_minute, _, _) in enumerate(
            self.answers.iloc[1:-2].itertuples(name=None)
        ):
            hour_into_session = open_minute + self.one_hour

            minute_before_session = open_minute - self.one_minute
            minute_after_session = close_minute + self.one_minute

            next_session_label = self.answers.index[idx + 2]
            previous_session_label = self.answers.index[idx]

            # verify that minutes inside a session resolve correctly
            minutes_that_resolve_to_this_session = [
                m(open_minute, _parse=False),
                m(open_minute, direction="next", _parse=False),
                m(open_minute, direction="previous", _parse=False),
                m(open_minute, direction="none", _parse=False),
                m(hour_into_session, _parse=False),
                m(hour_into_session, direction="next", _parse=False),
                m(hour_into_session, direction="previous", _parse=False),
                m(hour_into_session, direction="none", _parse=False),
                m(close_minute),
                m(close_minute, direction="next", _parse=False),
                m(close_minute, direction="previous", _parse=False),
                m(close_minute, direction="none", _parse=False),
                session_label,
            ]

            if self.GAPS_BETWEEN_SESSIONS:
                minutes_that_resolve_to_this_session.append(
                    m(minute_before_session, _parse=False)
                )
                minutes_that_resolve_to_this_session.append(
                    m(minute_before_session, direction="next", _parse=False)
                )

                minutes_that_resolve_to_this_session.append(
                    m(minute_after_session, direction="previous", _parse=False)
                )

            self.assertTrue(
                all(
                    x == minutes_that_resolve_to_this_session[0]
                    for x in minutes_that_resolve_to_this_session
                )
            )

            minutes_that_resolve_to_next_session = [
                m(minute_after_session, _parse=False),
                m(minute_after_session, direction="next", _parse=False),
                next_session_label,
            ]

            self.assertTrue(
                all(
                    x == minutes_that_resolve_to_next_session[0]
                    for x in minutes_that_resolve_to_next_session
                )
            )

            self.assertEqual(
                m(minute_before_session, direction="previous", _parse=False),
                previous_session_label,
            )

            if self.GAPS_BETWEEN_SESSIONS:
                # Make sure we use the cache correctly
                minutes_that_resolve_to_different_sessions = [
                    m(minute_after_session, direction="next", _parse=False),
                    m(minute_after_session, direction="previous", _parse=False),
                    m(minute_after_session, direction="next", _parse=False),
                ]

                self.assertEqual(
                    minutes_that_resolve_to_different_sessions,
                    [next_session_label, session_label, next_session_label],
                )

            # make sure that exceptions are raised at the right time
            with self.assertRaises(ValueError):
                m(open_minute, "asdf", _parse=False)

            if self.GAPS_BETWEEN_SESSIONS:
                with self.assertRaises(ValueError):
                    m(minute_before_session, direction="none", _parse=False)

        # minute is later than last session's close
        minute_after_last_close = self.answers.iloc[-1].market_close + self.one_minute
        session_label = self.answers.index[-1]

        minute_that_resolves_to_session_label = m(
            minute_after_last_close, direction="previous", _parse=False
        )

        self.assertEqual(session_label, minute_that_resolves_to_session_label)

        with self.assertRaises(ValueError):
            m(minute_after_last_close, _parse=False)
        with self.assertRaises(ValueError):
            m(minute_after_last_close, direction="next", _parse=False)
        with self.assertRaises(ValueError):
            m(minute_after_last_close, direction="none", _parse=False)

    @parameterized.expand(
        [
            (1, 0),
            (2, 0),
            (2, 1),
        ]
    )
    def test_minute_index_to_session_labels(self, interval, offset):
        minutes = self.calendar.minutes_for_sessions_in_range(
            self.MINUTE_INDEX_TO_SESSION_LABELS_START,
            self.MINUTE_INDEX_TO_SESSION_LABELS_END,
        )
        minutes = minutes[range(offset, len(minutes), interval)]

        np.testing.assert_array_equal(
            pd.DatetimeIndex(minutes.map(self.calendar.minute_to_session_label)),
            self.calendar.minute_index_to_session_labels(minutes),
        )

    def test_next_prev_session(self):
        session_labels = self.answers.index[1:-2]
        max_idx = len(session_labels) - 1

        # the very first session
        first_session_label = self.answers.index[0]
        with self.assertRaises(ValueError):
            self.calendar.previous_session_label(first_session_label, _parse=False)

        # all the sessions in the middle
        for idx, session_label in enumerate(session_labels):
            if idx < max_idx:
                self.assertEqual(
                    self.calendar.next_session_label(session_label, _parse=False),
                    session_labels[idx + 1],
                )

            if idx > 0:
                self.assertEqual(
                    self.calendar.previous_session_label(session_label, _parse=False),
                    session_labels[idx - 1],
                )

        # the very last session
        last_session_label = self.answers.index[-1]
        with self.assertRaises(ValueError):
            self.calendar.next_session_label(last_session_label, _parse=False)

    @staticmethod
    def _find_full_session(calendar):
        for session_label in calendar.schedule.index:
            if session_label not in calendar.early_closes:
                return session_label

        return None

    def test_minutes_for_period(self):
        # full session
        # find a session that isn't an early close.  start from the first
        # session, should be quick.
        full_session_label = self._find_full_session(self.calendar)
        if full_session_label is None:
            raise ValueError("Cannot find a full session to test!")

        minutes = self.calendar.minutes_for_session(full_session_label)
        _open, _close = self.calendar.open_and_close_for_session(full_session_label)
        _break_start, _break_end = self.calendar.break_start_and_end_for_session(
            full_session_label
        )
        if not pd.isnull(_break_start):
            constructed_minutes = np.concatenate(
                [
                    pd.date_range(start=_open, end=_break_start, freq="min"),
                    pd.date_range(start=_break_end, end=_close, freq="min"),
                ]
            )
        else:
            constructed_minutes = pd.date_range(start=_open, end=_close, freq="min")

        np.testing.assert_array_equal(
            minutes,
            constructed_minutes,
        )

        # early close period
        if self.HAVE_EARLY_CLOSES:
            early_close_session_label = self.calendar.early_closes[0]
            minutes_for_early_close = self.calendar.minutes_for_session(
                early_close_session_label
            )
            _open, _close = self.calendar.open_and_close_for_session(
                early_close_session_label
            )

            np.testing.assert_array_equal(
                minutes_for_early_close,
                pd.date_range(start=_open, end=_close, freq="min"),
            )

        # late open period
        if self.HAVE_LATE_OPENS:
            late_open_session_label = self.calendar.late_opens[0]
            minutes_for_late_open = self.calendar.minutes_for_session(
                late_open_session_label
            )
            _open, _close = self.calendar.open_and_close_for_session(
                late_open_session_label
            )

            np.testing.assert_array_equal(
                minutes_for_late_open,
                pd.date_range(start=_open, end=_close, freq="min"),
            )

    def test_sessions_in_range(self):
        # pick two sessions
        session_count = len(self.calendar.schedule.index)

        first_idx = session_count // 3
        second_idx = 2 * first_idx

        first_session_label = self.calendar.schedule.index[first_idx]
        second_session_label = self.calendar.schedule.index[second_idx]

        answer_key = self.calendar.schedule.index[first_idx : second_idx + 1]
        rtrn = self.calendar.sessions_in_range(
            first_session_label, second_session_label, _parse=False
        )

        np.testing.assert_array_equal(answer_key, rtrn)

    def get_session_block(self):
        """
        Get an "interesting" range of three sessions in a row. By default this
        tries to find and return a (full session, early close session, full
        session) block.
        """
        if not self.HAVE_EARLY_CLOSES:
            # If we don't have any early closes, just return a "random" chunk
            # of three sessions.
            return self.calendar.all_sessions[10:13]

        shortened_session = self.calendar.early_closes[0]
        shortened_session_idx = self.calendar.schedule.index.get_loc(shortened_session)

        session_before = self.calendar.schedule.index[shortened_session_idx - 1]
        session_after = self.calendar.schedule.index[shortened_session_idx + 1]

        return [session_before, shortened_session, session_after]

    def test_minutes_in_range(self):
        sessions = self.get_session_block()

        first_open, first_close = self.calendar.open_and_close_for_session(sessions[0])
        minute_before_first_open = first_open - self.one_minute

        middle_open, middle_close = self.calendar.open_and_close_for_session(
            sessions[1]
        )

        last_open, last_close = self.calendar.open_and_close_for_session(sessions[-1])
        minute_after_last_close = last_close + self.one_minute

        # get all the minutes between first_open and last_close
        minutes1 = self.calendar.minutes_in_range(first_open, last_close, _parse=False)
        minutes2 = self.calendar.minutes_in_range(
            minute_before_first_open, minute_after_last_close, _parse=False
        )

        if self.GAPS_BETWEEN_SESSIONS:
            np.testing.assert_array_equal(minutes1, minutes2)
        else:
            # if no gaps, then minutes2 should have 2 extra minutes
            np.testing.assert_array_equal(minutes1, minutes2[1:-1])

        # manually construct the minutes
        (
            first_break_start,
            first_break_end,
        ) = self.calendar.break_start_and_end_for_session(sessions[0])
        (
            middle_break_start,
            middle_break_end,
        ) = self.calendar.break_start_and_end_for_session(sessions[1])
        (
            last_break_start,
            last_break_end,
        ) = self.calendar.break_start_and_end_for_session(sessions[-1])

        intervals = [
            (first_open, first_break_start, first_break_end, first_close),
            (middle_open, middle_break_start, middle_break_end, middle_close),
            (last_open, last_break_start, last_break_end, last_close),
        ]
        all_minutes = []

        for _open, _break_start, _break_end, _close in intervals:
            if pd.isnull(_break_start):
                all_minutes.append(
                    pd.date_range(start=_open, end=_close, freq="min"),
                )
            else:
                all_minutes.append(
                    pd.date_range(start=_open, end=_break_start, freq="min"),
                )
                all_minutes.append(
                    pd.date_range(start=_break_end, end=_close, freq="min"),
                )
        all_minutes = np.concatenate(all_minutes)

        np.testing.assert_array_equal(all_minutes, minutes1)

    def test_minutes_for_sessions_in_range(self):
        sessions = self.get_session_block()

        minutes = self.calendar.minutes_for_sessions_in_range(sessions[0], sessions[-1])

        # do it manually
        session0_minutes = self.calendar.minutes_for_session(sessions[0])
        session1_minutes = self.calendar.minutes_for_session(sessions[1])
        session2_minutes = self.calendar.minutes_for_session(sessions[2])

        concatenated_minutes = np.concatenate(
            [session0_minutes.values, session1_minutes.values, session2_minutes.values]
        )

        np.testing.assert_array_equal(concatenated_minutes, minutes.values)

    def test_sessions_window(self):
        sessions = self.get_session_block()

        np.testing.assert_array_equal(
            self.calendar.sessions_window(sessions[0], len(sessions) - 1, _parse=False),
            self.calendar.sessions_in_range(sessions[0], sessions[-1], _parse=False),
        )

        np.testing.assert_array_equal(
            self.calendar.sessions_window(
                sessions[-1], -1 * (len(sessions) - 1), _parse=False
            ),
            self.calendar.sessions_in_range(sessions[0], sessions[-1], _parse=False),
        )

    def test_session_distance(self):
        sessions = self.get_session_block()

        forward_distance = self.calendar.session_distance(
            sessions[0],
            sessions[-1],
            _parse=False,
        )
        self.assertEqual(forward_distance, len(sessions))

        backward_distance = self.calendar.session_distance(
            sessions[-1],
            sessions[0],
            _parse=False,
        )
        self.assertEqual(backward_distance, -len(sessions))

        one_day_distance = self.calendar.session_distance(
            sessions[0],
            sessions[0],
            _parse=False,
        )
        self.assertEqual(one_day_distance, 1)

    def test_open_and_close_for_session(self):
        for session_label, open_answer, close_answer, _, _ in self.answers.itertuples(
            name=None
        ):

            found_open, found_close = self.calendar.open_and_close_for_session(
                session_label, _parse=False
            )

            # Test that the methods for just session open and close produce the
            # same values as the method for getting both.
            alt_open = self.calendar.session_open(session_label, _parse=False)
            self.assertEqual(alt_open, found_open)

            alt_close = self.calendar.session_close(session_label, _parse=False)
            self.assertEqual(alt_close, found_close)

            self.assertEqual(open_answer, found_open)
            self.assertEqual(close_answer, found_close)

    def test_session_opens_in_range(self):
        found_opens = self.calendar.session_opens_in_range(
            self.answers.index[0],
            self.answers.index[-1],
            _parse=False,
        )
        found_opens.index.freq = None
        tm.assert_series_equal(found_opens, self.answers["market_open"])

    def test_session_closes_in_range(self):
        found_closes = self.calendar.session_closes_in_range(
            self.answers.index[0],
            self.answers.index[-1],
            _parse=False,
        )
        found_closes.index.freq = None
        tm.assert_series_equal(found_closes, self.answers["market_close"])

    def test_daylight_savings(self):
        # 2004 daylight savings switches:
        # Sunday 2004-04-04 and Sunday 2004-10-31

        # make sure there's no weirdness around calculating the next day's
        # session's open time.

        m = dict(self.calendar.open_times)
        m[pd.Timestamp.min] = m.pop(None)
        open_times = pd.Series(m)

        for date in self.DAYLIGHT_SAVINGS_DATES:
            next_day = pd.Timestamp(date, tz=UTC)
            open_date = next_day + Timedelta(days=self.calendar.open_offset)

            the_open = self.calendar.schedule.loc[next_day].market_open

            localized_open = the_open.tz_localize(UTC).tz_convert(self.calendar.tz)

            self.assertEqual(
                (open_date.year, open_date.month, open_date.day),
                (localized_open.year, localized_open.month, localized_open.day),
            )

            open_ix = open_times.index.searchsorted(pd.Timestamp(date), side="right")
            if open_ix == len(open_times):
                open_ix -= 1

            self.assertEqual(open_times.iloc[open_ix].hour, localized_open.hour)

            self.assertEqual(open_times.iloc[open_ix].minute, localized_open.minute)

    def test_start_end(self):
        """
        Check ExchangeCalendar with defined start/end dates.
        """
        calendar = self.calendar_class(
            start=self.TEST_START_END_FIRST,
            end=self.TEST_START_END_LAST,
        )

        self.assertEqual(
            calendar.first_trading_session,
            self.TEST_START_END_EXPECTED_FIRST,
        )
        self.assertEqual(
            calendar.last_trading_session,
            self.TEST_START_END_EXPECTED_LAST,
        )

    def test_has_breaks(self):
        has_breaks = self.calendar.has_breaks()
        self.assertEqual(has_breaks, self.HAVE_BREAKS)

    def test_session_has_break(self):
        if self.SESSION_WITHOUT_BREAK is not None:
            self.assertFalse(
                self.calendar.session_has_break(self.SESSION_WITHOUT_BREAK)
            )
        if self.SESSION_WITH_BREAK is not None:
            self.assertTrue(self.calendar.session_has_break(self.SESSION_WITH_BREAK))


class EuronextCalendarTestBase(ExchangeCalendarTestBase):
    """
    Shared tests for countries on the Euronext exchange.
    """

    # Early close is 2:05 PM.
    # Source: https://www.euronext.com/en/calendars-hours
    TIMEDELTA_TO_EARLY_CLOSE = pd.Timedelta(hours=14, minutes=5)

    def test_normal_year(self):
        expected_holidays_2014 = [
            pd.Timestamp("2014-01-01", tz=UTC),  # New Year's Day
            pd.Timestamp("2014-04-18", tz=UTC),  # Good Friday
            pd.Timestamp("2014-04-21", tz=UTC),  # Easter Monday
            pd.Timestamp("2014-05-01", tz=UTC),  # Labor Day
            pd.Timestamp("2014-12-25", tz=UTC),  # Christmas
            pd.Timestamp("2014-12-26", tz=UTC),  # Boxing Day
        ]

        for session_label in expected_holidays_2014:
            self.assertNotIn(session_label, self.calendar.all_sessions)

        early_closes_2014 = [
            pd.Timestamp("2014-12-24", tz=UTC),  # Christmas Eve
            pd.Timestamp("2014-12-31", tz=UTC),  # New Year's Eve
        ]

        for early_close_session_label in early_closes_2014:
            self.assertIn(
                early_close_session_label,
                self.calendar.early_closes,
            )

    def test_holidays_fall_on_weekend(self):
        # Holidays falling on a weekend should not be made up during the week.
        expected_sessions = [
            # In 2010, Labor Day fell on a Saturday, so the market should be
            # open on both the prior Friday and the following Monday.
            pd.Timestamp("2010-04-30", tz=UTC),
            pd.Timestamp("2010-05-03", tz=UTC),
            # Christmas also fell on a Saturday, meaning Boxing Day fell on a
            # Sunday. The market should still be open on both the prior Friday
            # and the following Monday.
            pd.Timestamp("2010-12-24", tz=UTC),
            pd.Timestamp("2010-12-27", tz=UTC),
        ]

        for session_label in expected_sessions:
            self.assertIn(session_label, self.calendar.all_sessions)

    def test_half_days(self):
        half_days = [
            # In 2010, Christmas Eve and NYE are on Friday, so they should be
            # half days.
            pd.Timestamp("2010-12-24", tz=self.TZ),
            pd.Timestamp("2010-12-31", tz=self.TZ),
        ]
        full_days = [
            # In Dec 2011, Christmas Eve and NYE were both on a Saturday, so
            # the preceding Fridays should be full days.
            pd.Timestamp("2011-12-23", tz=self.TZ),
            pd.Timestamp("2011-12-30", tz=self.TZ),
        ]

        for half_day in half_days:
            half_day_close_time = self.calendar.next_close(half_day)
            self.assertEqual(
                half_day_close_time,
                half_day + self.TIMEDELTA_TO_EARLY_CLOSE,
            )
        for full_day in full_days:
            full_day_close_time = self.calendar.next_close(full_day)
            self.assertEqual(
                full_day_close_time,
                full_day + self.TIMEDELTA_TO_NORMAL_CLOSE,
            )


class OpenDetectionTestCase(TestCase):
    # This is an extra set of unit tests that were added during a rewrite of
    # `minute_index_to_session_labels` to ensure that the existing
    # calendar-generic test suite correctly covered edge cases around
    # non-market minutes.
    def test_detect_non_market_minutes(self):
        cal = get_calendar("NYSE")
        # NOTE: This test is here instead of being on the base class for all
        # calendars because some of our calendars are 24/7, which means there
        # aren't any non-market minutes to find.
        day0 = cal.minutes_for_sessions_in_range(
            pd.Timestamp("2013-07-03", tz=UTC),
            pd.Timestamp("2013-07-03", tz=UTC),
        )
        for minute in day0:
            self.assertTrue(cal.is_open_on_minute(minute))

        day1 = cal.minutes_for_sessions_in_range(
            pd.Timestamp("2013-07-05", tz=UTC),
            pd.Timestamp("2013-07-05", tz=UTC),
        )
        for minute in day1:
            self.assertTrue(cal.is_open_on_minute(minute))

        def NYSE_timestamp(s):
            return pd.Timestamp(s, tz="America/New_York").tz_convert(UTC)

        non_market = [
            # After close.
            NYSE_timestamp("2013-07-03 16:01"),
            # Holiday.
            NYSE_timestamp("2013-07-04 10:00"),
            # Before open.
            NYSE_timestamp("2013-07-05 9:29"),
        ]
        for minute in non_market:
            self.assertFalse(cal.is_open_on_minute(minute), minute)

            input_ = pd.to_datetime(
                np.hstack([day0.values, minute.asm8, day1.values]),
                utc=True,
            )
            with self.assertRaises(ValueError) as e:
                cal.minute_index_to_session_labels(input_)

            exc_str = str(e.exception)
            self.assertIn("First Bad Minute: {}".format(minute), exc_str)


class NoDSTExchangeCalendarTestBase(ExchangeCalendarTestBase):
    def test_daylight_savings(self):
        """
        Several countries in Africa / Asia do not observe DST
        so we need to skip over this test for those markets
        """
        pass


def get_csv(name: str) -> pd.DataFrame:
    """Get csv file as DataFrame for given calendar `name`."""
    filename = name.replace("/", "-").lower() + ".csv"
    path = pathlib.Path(__file__).parent.joinpath("resources", filename)

    df = pd.read_csv(
        path,
        index_col=0,
        parse_dates=[0, 1, 2, 3, 4],
        infer_datetime_format=True,
    )
    df.index = df.index.tz_localize("UTC")
    for col in df:
        df[col] = df[col].dt.tz_localize("UTC")
    return df


class Answers:
    """Inputs and expected output for testing a given calendar and side.

    Inputs and expected outputs are provided by public instance methods and
    properties. These either read directly from the corresponding .csv file
    or are evaluated from the .csv file contents. NB Properites / methods
    MUST NOT make evaluations by way of repeating the code of the
    ExchangeCalendar method they are intended to test!

    Parameters
    ----------
    calendar_name
        Canonical name of calendar for which require answer info. For
        example, 'XNYS'.

    side {'both', 'left', 'right', 'neither'}
        Side of sessions to treat as trading minutes.
    """

    ONE_MIN = pd.Timedelta(1, "T")
    TWO_MIN = pd.Timedelta(2, "T")
    ONE_DAY = pd.Timedelta(1, "D")

    LEFT_SIDES = ["left", "both"]
    RIGHT_SIDES = ["right", "both"]

    def __init__(
        self,
        calendar_name: str,
        side: str,
    ):
        self._name = calendar_name.upper()
        self._side = side

    # TODO. When new test suite completed, review Answers to remove any
    # unused properties / methods.

    # exposed constructor arguments

    @property
    def name(self) -> str:
        """Name of corresponding calendar."""
        return self._name

    @property
    def side(self) -> str:
        """Side of calendar for which answers valid."""
        return self._side

    # properties read (indirectly) from csv file

    @functools.lru_cache(maxsize=4)
    def _answers(self) -> pd.DataFrame:
        return get_csv(self.name)

    @property
    def answers(self) -> pd.DataFrame:
        """Answers as correspoding csv."""
        return self._answers()

    @property
    def sessions(self) -> pd.DatetimeIndex:
        """Session labels."""
        return self.answers.index

    @property
    def opens(self) -> pd.Series:
        """Market open time for each session."""
        return self.answers.market_open

    @property
    def closes(self) -> pd.Series:
        """Market close time for each session."""
        return self.answers.market_close

    @property
    def break_starts(self) -> pd.Series:
        """Break start time for each session."""
        return self.answers.break_start

    @property
    def break_ends(self) -> pd.Series:
        """Break end time for each session."""
        return self.answers.break_end

    # get and helper methods

    def get_session_open(self, session: pd.Timestamp) -> pd.Timestamp:
        """Open for `session`."""
        return self.opens[session]

    def get_session_close(self, session: pd.Timestamp) -> pd.Timestamp:
        """Close for `session`."""
        return self.closes[session]

    def get_session_break_start(self, session: pd.Timestamp) -> pd.Timestamp | pd.NaT:
        """Break start for `session`."""
        return self.break_starts[session]

    def get_session_break_end(self, session: pd.Timestamp) -> pd.Timestamp | pd.NaT:
        """Break end for `session`."""
        return self.break_ends[session]

    def get_session_first_trading_minute(self, session: pd.Timestamp) -> pd.Timestamp:
        """First trading minute of `session`."""
        open_ = self.get_session_open(session)
        return open_ if self.side in self.LEFT_SIDES else open_ + self.ONE_MIN

    def get_session_last_trading_minute(self, session: pd.Timestamp) -> pd.Timestamp:
        """Last trading minute of `session`."""
        close = self.get_session_close(session)
        return close if self.side in self.RIGHT_SIDES else close - self.ONE_MIN

    def get_session_last_am_minute(
        self, session: pd.Timestamp
    ) -> pd.Timestamp | pd.NaT:
        """Last trading minute of am subsession of `session`."""
        break_start = self.get_session_break_start(session)
        if pd.isna(break_start):
            return pd.NaT
        return (
            break_start if self.side in self.RIGHT_SIDES else break_start - self.ONE_MIN
        )

    def get_session_first_pm_minute(
        self, session: pd.Timestamp
    ) -> pd.Timestamp | pd.NaT:
        """First trading minute of pm subsession of `session`."""
        break_end = self.get_session_break_end(session)
        if pd.isna(break_end):
            return pd.NaT
        return break_end if self.side in self.LEFT_SIDES else break_end + self.ONE_MIN

    def get_next_session(self, session: pd.Timestamp) -> pd.Timestamp:
        """Get session that immediately follows `session`."""
        assert (
            session != self.last_session
        ), "Cannot get session later than last answers' session."
        idx = self.sessions.get_loc(session) + 1
        return self.sessions[idx]

    def get_next_sessions(
        self, session: pd.Timestamp, count: int = 1
    ) -> pd.DatetimeIndex:
        """Get sessions that immediately follow `session`.

        count : default: 1
            Number of sessions following `session` to get.
        """
        assert count > 0 and session in self.sessions
        assert (
            session not in self.sessions[-count:]
        ), "Cannot get session later than last answers' session."
        idx = self.sessions.get_loc(session) + 1
        return self.sessions[idx : idx + count]

    @staticmethod
    def get_sessions_sample(sessions: pd.DatetimeIndex):
        """Return sample of given `sessions`.

        Sample includes:
            All sessions within first two years of `sessions`.
            All sessions within last two years of `sessions`.
            All sessions falling:
                within first 3 days of any month.
                from 28th of any month.
                from 14th through 16th of any month.
        """
        if sessions.empty:
            return sessions

        mask = (
            (sessions < sessions[0] + pd.DateOffset(years=2))
            | (sessions > sessions[-1] - pd.DateOffset(years=2))
            | (sessions.day <= 3)
            | (sessions.day >= 28)
            | (14 <= sessions.day) & (sessions.day <= 16)
        )
        return sessions[mask]

    def get_sessions_minutes(
        self, start: pd.Timestamp, end: pd.Timestamp | int = 1
    ) -> pd.DatetimeIndex:
        """Get trading minutes for 1 or more consecutive sessions.

        Parameters
        ----------
        start
            Session from which to get trading minutes.
        end
            Session through which to get trading mintues. Can be passed as:
                pd.Timestamp: return will include trading minutes for `end`
                    session.
                int: where int represents number of consecutive sessions
                    inclusive of `start`, for which require trading
                    minutes. Default is 1, such that by default will return
                    trading minutes for only `start` session.
        """
        idx = self.sessions.get_loc(start)
        stop = idx + end if isinstance(end, int) else self.sessions.get_loc(end) + 1
        indexer = slice(idx, stop)

        dtis = []
        for first, last, last_am, first_pm in zip(
            self.first_minutes[indexer],
            self.last_minutes[indexer],
            self.last_am_minutes[indexer],
            self.first_pm_minutes[indexer],
        ):
            if pd.isna(last_am):
                dtis.append(pd.date_range(first, last, freq="T"))
            else:
                dtis.append(pd.date_range(first, last_am, freq="T"))
                dtis.append(pd.date_range(first_pm, last, freq="T"))

        return dtis[0].union_many(dtis[1:])

    # general evaluated properties

    @functools.lru_cache(maxsize=4)
    def _has_a_break(self) -> pd.DatetimeIndex:
        return self.break_starts.notna().any()

    @property
    def has_a_break(self) -> bool:
        """Does any session of answers have a break."""
        return self._has_a_break()

    @functools.lru_cache(maxsize=4)
    def _first_minutes(self) -> pd.Series:
        if self.side in self.LEFT_SIDES:
            minutes = self.opens.copy()
        else:
            minutes = self.opens + self.ONE_MIN
        minutes.name = "first_minutes"
        return minutes

    @property
    def first_minutes(self) -> pd.Series:
        """First trading minute of each session (UTC)."""
        return self._first_minutes()

    @property
    def first_minutes_plus_one(self) -> pd.Series:
        """First trading minute of each session plus one minute."""
        return self.first_minutes + self.ONE_MIN

    @property
    def first_minutes_less_one(self) -> pd.Series:
        """First trading minute of each session less one minute."""
        return self.first_minutes - self.ONE_MIN

    @functools.lru_cache(maxsize=4)
    def _last_minutes(self) -> pd.Series:
        if self.side in self.RIGHT_SIDES:
            minutes = self.closes.copy()
        else:
            minutes = self.closes - self.ONE_MIN
        minutes.name = "last_minutes"
        return minutes

    @property
    def last_minutes(self) -> pd.Series:
        """Last trading minute of each session."""
        return self._last_minutes()

    @property
    def last_minutes_plus_one(self) -> pd.Series:
        """Last trading minute of each session plus one minute."""
        return self.last_minutes + self.ONE_MIN

    @property
    def last_minutes_less_one(self) -> pd.Series:
        """Last trading minute of each session less one minute."""
        return self.last_minutes - self.ONE_MIN

    @functools.lru_cache(maxsize=4)
    def _last_am_minutes(self) -> pd.Series:
        if self.side in self.RIGHT_SIDES:
            minutes = self.break_starts.copy()
        else:
            minutes = self.break_starts - self.ONE_MIN
        minutes.name = "last_am_minutes"
        return minutes

    @property
    def last_am_minutes(self) -> pd.Series:
        """Last pre-break trading minute of each session.

        NaT if session does not have a break.
        """
        return self._last_am_minutes()

    @property
    def last_am_minutes_plus_one(self) -> pd.Series:
        """Last pre-break trading minute of each session plus one minute."""
        return self.last_am_minutes + self.ONE_MIN

    @property
    def last_am_minutes_less_one(self) -> pd.Series:
        """Last pre-break trading minute of each session less one minute."""
        return self.last_am_minutes - self.ONE_MIN

    @functools.lru_cache(maxsize=4)
    def _first_pm_minutes(self) -> pd.Series:
        if self.side in self.LEFT_SIDES:
            minutes = self.break_ends.copy()
        else:
            minutes = self.break_ends + self.ONE_MIN
        minutes.name = "first_pm_minutes"
        return minutes

    @property
    def first_pm_minutes(self) -> pd.Series:
        """First post-break trading minute of each session.

        NaT if session does not have a break.
        """
        return self._first_pm_minutes()

    @property
    def first_pm_minutes_plus_one(self) -> pd.Series:
        """First post-break trading minute of each session plus one minute."""
        return self.first_pm_minutes + self.ONE_MIN

    @property
    def first_pm_minutes_less_one(self) -> pd.Series:
        """First post-break trading minute of each session less one minute."""
        return self.first_pm_minutes - self.ONE_MIN

    # evaluated properties for sessions

    @property
    def _mask_breaks(self) -> pd.Series:
        return self.break_starts.notna()

    @functools.lru_cache(maxsize=4)
    def _sessions_with_break(self) -> pd.DatetimeIndex:
        return self.sessions[self._mask_breaks]

    @property
    def sessions_with_break(self) -> pd.DatetimeIndex:
        return self._sessions_with_break()

    @functools.lru_cache(maxsize=4)
    def _sessions_without_break(self) -> pd.DatetimeIndex:
        return self.sessions[~self._mask_breaks]

    @property
    def sessions_without_break(self) -> pd.DatetimeIndex:
        return self._sessions_without_break()

    def session_has_break(self, session: pd.Timestamp) -> bool:
        """Query if `session` has a break."""
        return session in self.sessions_with_break

    @property
    def _mask_sessions_without_gap_after(self) -> pd.Series:
        if self.side == "neither":
            # will always have gap after if neither open or close are trading
            # minutes (assuming sessions cannot overlap)
            return pd.Series(False, index=self.sessions)

        elif self.side == "both":
            # a trading minute cannot be a minute of more than one session.
            assert not (self.closes == self.opens.shift(-1)).any()
            # there will be no gap if next open is one minute after previous close
            closes_plus_min = self.closes + pd.Timedelta(1, "T")
            return self.opens.shift(-1) == closes_plus_min

        else:
            return self.opens.shift(-1) == self.closes

    @property
    def _mask_sessions_without_gap_before(self) -> pd.Series:
        if self.side == "neither":
            # will always have gap before if neither open or close are trading
            # minutes (assuming sessions cannot overlap)
            return pd.Series(False, index=self.sessions)

        elif self.side == "both":
            # a trading minute cannot be a minute of more than one session.
            assert not (self.closes == self.opens.shift(-1)).any()
            # there will be no gap if previous close is one minute before next open
            opens_minus_one = self.opens - pd.Timedelta(1, "T")
            return self.closes.shift(1) == opens_minus_one

        else:
            return self.closes.shift(1) == self.opens

    @functools.lru_cache(maxsize=4)
    def _sessions_without_gap_after(self) -> pd.DatetimeIndex:
        mask = self._mask_sessions_without_gap_after
        return self.sessions[mask][:-1]

    @property
    def sessions_without_gap_after(self) -> pd.DatetimeIndex:
        """Sessions not followed by a non-trading minute.

        Rather, sessions immediately followed by first trading minute of
        next session.
        """
        return self._sessions_without_gap_after()

    @functools.lru_cache(maxsize=4)
    def _sessions_with_gap_after(self) -> pd.DatetimeIndex:
        mask = self._mask_sessions_without_gap_after
        return self.sessions[~mask][:-1]

    @property
    def sessions_with_gap_after(self) -> pd.DatetimeIndex:
        """Sessions followed by a non-trading minute."""
        return self._sessions_with_gap_after()

    @functools.lru_cache(maxsize=4)
    def _sessions_without_gap_before(self) -> pd.DatetimeIndex:
        mask = self._mask_sessions_without_gap_before
        return self.sessions[mask][1:]

    @property
    def sessions_without_gap_before(self) -> pd.DatetimeIndex:
        """Sessions not preceeded by a non-trading minute.

        Rather, sessions immediately preceeded by last trading minute of
        previous session.
        """
        return self._sessions_without_gap_before()

    @functools.lru_cache(maxsize=4)
    def _sessions_with_gap_before(self) -> pd.DatetimeIndex:
        mask = self._mask_sessions_without_gap_before
        return self.sessions[~mask][1:]

    @property
    def sessions_with_gap_before(self) -> pd.DatetimeIndex:
        """Sessions preceeded by a non-trading minute."""
        return self._sessions_with_gap_before()

    # evaluated properties for first and last sessions

    @property
    def first_session(self) -> pd.Timestamp:
        """First session covered by answers."""
        return self.sessions[0]

    @property
    def last_session(self) -> pd.Timestamp:
        """Last session covered by answers."""
        return self.sessions[-1]

    @property
    def first_trading_minute(self) -> pd.Timestamp:
        return self.get_session_first_trading_minute(self.first_session)

    @property
    def last_trading_minute(self) -> pd.Timestamp:
        return self.get_session_last_trading_minute(self.last_session)

    # times are changing...

    @functools.lru_cache(maxsize=16)
    def _get_sessions_with_times_different_to_a_contiguous_session(
        self,
        column: str,  # typing.Literal["opens", "closes", "break_starts", "break_ends"]
    ) -> list[pd.DatetimeIndex]:
        """For a given answers column, get session labels where time differs from
        time of next session.

        Where `column` is a break time ("break_starts" or "break_ends"), return
        will not include sessions when next/prev session has a different `has_break`
        status. For example, if session_0 has a break and session_1 does not have
        a break, or vice versa, then session_0 will not be included to return. For
        sessions followed by a session with a different `has_break` status, see
        `_get_sessions_with_has_break_different_to_next_session`.

        Returns
        -------
        list of pd.Datetimeindex
            [0] sessions with earlier next session
            [1] sessions with later next session
            [2] sessions with previous session later
            [3] sessions with previous session earlier
        """
        # column takes string to allow lru_cache (Series not hashable)

        is_break_col = column[0] == "b"
        column_ = getattr(self, column)

        if is_break_col:
            if column_.isna().all():
                return [pd.DatetimeIndex([], tz="UTC")] * 4
            column_ = column_.fillna(method="ffill").fillna(method="bfill")

        diff = (column_.shift(-1) - column_)[:-1]
        remainder = diff % pd.Timedelta(hours=24)
        mask = remainder != pd.Timedelta(0)
        sessions = self.sessions[:-1][mask]
        next_session_earlier_mask = remainder[mask] > pd.Timedelta(hours=12)
        next_session_earlier = sessions[next_session_earlier_mask]
        next_session_later = sessions[~next_session_earlier_mask]

        if is_break_col:
            mask = next_session_earlier.isin(self.sessions_without_break)
            next_session_earlier = next_session_earlier.drop(next_session_earlier[mask])
            mask = next_session_later.isin(self.sessions_without_break)
            next_session_later = next_session_later.drop(next_session_later[mask])

        indices = self.sessions.get_indexer(next_session_earlier) + 1
        previous_session_later = self.sessions[indices]

        indices = self.sessions.get_indexer(next_session_later) + 1
        previous_session_earlier = self.sessions[indices]

        return [
            next_session_earlier,
            next_session_later,
            previous_session_later,
            previous_session_earlier,
        ]

    @property
    def _sessions_with_opens_different_to_a_contiguous_session(
        self,
    ) -> list[pd.DatetimeIndex]:
        return self._get_sessions_with_times_different_to_a_contiguous_session("opens")

    @property
    def _sessions_with_closes_different_to_a_contiguous_session(
        self,
    ) -> list[pd.DatetimeIndex]:
        return self._get_sessions_with_times_different_to_a_contiguous_session("closes")

    @property
    def _sessions_with_break_start_different_to_a_contiguous_session(
        self,
    ) -> list[pd.DatetimeIndex]:
        return self._get_sessions_with_times_different_to_a_contiguous_session(
            "break_starts"
        )

    @property
    def _sessions_with_break_end_different_to_a_contiguous_session(
        self,
    ) -> list[pd.DatetimeIndex]:
        return self._get_sessions_with_times_different_to_a_contiguous_session(
            "break_ends"
        )

    @property
    def sessions_next_open_earlier(self) -> pd.DatetimeIndex:
        return self._sessions_with_opens_different_to_a_contiguous_session[0]

    @property
    def sessions_next_open_later(self) -> pd.DatetimeIndex:
        return self._sessions_with_opens_different_to_a_contiguous_session[1]

    @property
    def sessions_next_open_different(self) -> pd.DatetimeIndex:
        return self.sessions_next_open_earlier.union(self.sessions_next_open_later)

    @property
    def sessions_next_close_earlier(self) -> pd.DatetimeIndex:
        return self._sessions_with_closes_different_to_a_contiguous_session[0]

    @property
    def sessions_next_close_later(self) -> pd.DatetimeIndex:
        return self._sessions_with_closes_different_to_a_contiguous_session[1]

    @property
    def sessions_next_close_different(self) -> pd.DatetimeIndex:
        return self.sessions_next_close_earlier.union(self.sessions_next_close_later)

    @property
    def sessions_next_break_start_earlier(self) -> pd.DatetimeIndex:
        return self._sessions_with_break_start_different_to_a_contiguous_session[0]

    @property
    def sessions_next_break_start_later(self) -> pd.DatetimeIndex:
        return self._sessions_with_break_start_different_to_a_contiguous_session[1]

    @property
    def sessions_next_break_start_different(self) -> pd.DatetimeIndex:
        earlier = self.sessions_next_break_start_earlier
        later = self.sessions_next_break_start_later
        return earlier.union(later)

    @property
    def sessions_next_break_end_earlier(self) -> pd.DatetimeIndex:
        return self._sessions_with_break_end_different_to_a_contiguous_session[0]

    @property
    def sessions_next_break_end_later(self) -> pd.DatetimeIndex:
        return self._sessions_with_break_end_different_to_a_contiguous_session[1]

    @property
    def sessions_next_break_end_different(self) -> pd.DatetimeIndex:
        earlier = self.sessions_next_break_end_earlier
        later = self.sessions_next_break_end_later
        return earlier.union(later)

    @functools.lru_cache(maxsize=4)
    def _get_sessions_with_has_break_different_to_next_session(
        self,
    ) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
        """Get sessions with 'has_break' different to next session.

        Returns
        -------
        tuple[pd.DatetimeIndex, pd.DatetimeIndex]
            [0] Sessions that have a break and are immediately followed by
            a session which does not have a break.
            [1] Sessions that do not have a break and are immediately
            followed by a session which does have a break.
        """
        mask = (self.break_starts.notna() & self.break_starts.shift(-1).isna())[:-1]
        sessions_with_break_next_session_without_break = self.sessions[:-1][mask]

        mask = (self.break_starts.isna() & self.break_starts.shift(-1).notna())[:-1]
        sessions_without_break_next_session_with_break = self.sessions[:-1][mask]

        return (
            sessions_with_break_next_session_without_break,
            sessions_without_break_next_session_with_break,
        )

    @property
    def sessions_with_break_next_session_without_break(self) -> pd.DatetimeIndex:
        return self._get_sessions_with_has_break_different_to_next_session()[0]

    @property
    def sessions_without_break_next_session_with_break(self) -> pd.DatetimeIndex:
        return self._get_sessions_with_has_break_different_to_next_session()[1]

    @functools.lru_cache(maxsize=4)
    def _sessions_next_time_different(self) -> pd.DatetimeIndex:
        return self.sessions_next_open_different.union_many(
            [
                self.sessions_next_close_different,
                self.sessions_next_break_start_different,
                self.sessions_next_break_end_different,
                self.sessions_with_break_next_session_without_break,
                self.sessions_without_break_next_session_with_break,
            ]
        )

    @property
    def sessions_next_time_different(self) -> pd.DatetimeIndex:
        """Sessions where next session has a different time for any column.

        Includes sessions where next session has a different `has_break`
        status.
        """
        return self._sessions_next_time_different()

    # session blocks

    def _create_changing_times_session_block(
        self, session: pd.Timestamp
    ) -> pd.DatetimeIndex:
        """Create block of sessions with changing times.

        Given a `session` known to have at least one time (open, close,
        break_start or break_end) different from the next session, returns
        a block of consecutive sessions ending with the first session after
        `session` that has the same times as the session that immediately
        preceeds it (i.e. the last two sessions of the block will have the
        same times), or the last calendar session.
        """
        start_idx = self.sessions.get_loc(session)
        end_idx = start_idx + 1
        while self.sessions[end_idx] in self.sessions_next_time_different:
            end_idx += 1
        end_idx += 2  # +1 to include session with same times, +1 to serve as end index
        return self.sessions[start_idx:end_idx]

    def _get_normal_session_block(self) -> pd.DatetimeIndex:
        """Block of 3 sessions with unchanged timings."""
        start_idx = len(self.sessions) // 3
        end_idx = start_idx + 21
        for i in range(start_idx, end_idx):
            times_1 = self.answers.iloc[i].dt.time
            times_2 = self.answers.iloc[i + 1].dt.time
            times_3 = self.answers.iloc[i + 2].dt.time
            one_and_two_equal = (times_1 == times_2) | (times_1.isna() & times_2.isna())
            one_and_three_equal = (times_1 == times_3) | (
                times_1.isna() & times_3.isna()
            )
            if (one_and_two_equal & one_and_three_equal).all():
                break
            assert i < (end_idx - 1), "Unable to evaluate a normal session block!"
        return self.sessions[i : i + 3]

    def _get_session_block(
        self, from_session_of: pd.DatetimeIndex, to_session_of: pd.DatetimeIndex
    ) -> pd.DatetimeIndex:
        """Get session block with bounds defined by sessions of given indexes.

        Block will start with middle session of `from_session_of`.

        Block will run to the nearest subsequent session of `to_session_of`
        (or `self.final_session` if this comes first). Block will end with
        the session that immedidately follows this session.
        """
        i = len(from_session_of) // 2
        start_session = from_session_of[i]

        start_idx = self.sessions.get_loc(start_session)
        end_idx = start_idx + 1
        end_session = self.sessions[end_idx]

        while end_session not in to_session_of and end_session != self.last_session:
            end_idx += 1
            end_session = self.sessions[end_idx]

        return self.sessions[start_idx : end_idx + 2]

    @functools.lru_cache(maxsize=4)
    def _session_blocks(self) -> dict[str, pd.DatetimeIndex]:
        blocks = {}
        blocks["normal"] = self._get_normal_session_block()
        blocks["first_three"] = self.sessions[:3]
        blocks["last_three"] = self.sessions[-3:]

        # blocks here include where:
        #     session 1 has at least one different time from session 0
        #     session 0 has a break and session 1 does not (and vice versa)
        sessions_indexes = (
            ("next_open_earlier", self.sessions_next_open_earlier),
            ("next_open_later", self.sessions_next_open_later),
            ("next_close_earlier", self.sessions_next_close_earlier),
            ("next_close_later", self.sessions_next_close_later),
            ("next_break_start_earlier", self.sessions_next_break_start_earlier),
            ("next_break_start_later", self.sessions_next_break_start_later),
            ("next_break_end_earlier", self.sessions_next_break_end_earlier),
            ("next_break_end_later", self.sessions_next_break_end_later),
            (
                "with_break_to_without_break",
                self.sessions_with_break_next_session_without_break,
            ),
            (
                "without_break_to_with_break",
                self.sessions_without_break_next_session_with_break,
            ),
        )

        for name, index in sessions_indexes:
            if index.empty:
                blocks[name] = pd.DatetimeIndex([], tz="UTC")
            else:
                session = index[0]
                blocks[name] = self._create_changing_times_session_block(session)

        # blocks here move from session with gap to session without gap and vice versa
        if (not self.sessions_with_gap_after.empty) and (
            not self.sessions_without_gap_after.empty
        ):
            without_gap_to_with_gap = self._get_session_block(
                self.sessions_without_gap_after, self.sessions_with_gap_after
            )
            with_gap_to_without_gap = self._get_session_block(
                self.sessions_with_gap_after, self.sessions_without_gap_after
            )
        else:
            without_gap_to_with_gap = pd.DatetimeIndex([], tz="UTC")
            with_gap_to_without_gap = pd.DatetimeIndex([], tz="UTC")

        blocks["without_gap_to_with_gap"] = without_gap_to_with_gap
        blocks["with_gap_to_without_gap"] = with_gap_to_without_gap

        return blocks

    @property
    def session_blocks(self) -> dict[str, pd.DatetimeIndex]:
        """Dictionary of session blocks of a particular behaviour.

        Keys:
            "normal" - three sessions with unchanging timings.
            "first_three" - answers' first three sessions.
            "last_three" - answers's last three sessions.
            "next_open_earlier" - session 1 open is earlier than session 0
                open.
            "next_open_later" - session 1 open is later than session 0
                open.
            "next_close_earlier" - session 1 close is earlier than session
                0 close.
            "next_close_later" - session 1 close is later than session 0
                close.
            "next_break_start_earlier" - session 1 break_start is earlier
                than session 0 break_start.
            "next_break_start_later" - session 1 break_start is later than
                session 0 break_start.
            "next_break_end_earlier" - session 1 break_end is earlier than
                session 0 break_end.
            "next_break_end_later" - session 1 break_end is later than
                session 0 break_end.
            "with_break_to_without_break" - session 0 has a break, session
                1 does not have a break.
            "without_break_to_with_break" - session 0 does not have a
                break, session 1 does have a break.
            "without_gap_to_with_gap" - session 0 is not followed by a
                gap, session -2 is followed by a gap, session -1 is
                preceeded by a gap.
            "with_gap_to_without_gap" - session 0 is followed by a gap,
                session -2 is not followed by a gap, session -1 is not
                preceeded by a gap.

        If no such session block exists for any key then value will take an
        empty DatetimeIndex (UTC).
        """
        return self._session_blocks()

    def session_block_generator(self) -> abc.Iterator[tuple[str, pd.DatetimeIndex]]:
        """Generator of session blocks of a particular behaviour."""
        for name, block in self.session_blocks.items():
            if not block.empty:
                yield (name, block)

    @functools.lru_cache(maxsize=4)
    def _session_block_minutes(self) -> dict[str, pd.DatetimeIndex]:
        d = {}
        for name, block in self.session_blocks.items():
            if block.empty:
                d[name] = pd.DatetimeIndex([], tz="UTC")
                continue
            d[name] = self.get_sessions_minutes(block[0], len(block))
        return d

    @property
    def session_block_minutes(self) -> dict[str, pd.DatetimeIndex]:
        """Trading minutes for each `session_block`.

        Key:
            Session block name as documented to `session_blocks`.
        Value:
            Trading minutes of corresponding session block.
        """
        return self._session_block_minutes()

    # evaluated properties for minutes

    @functools.lru_cache(maxsize=4)
    def _evaluate_trading_and_break_minutes(self) -> tuple[tuple, tuple]:
        sessions = self.get_sessions_sample(self.sessions)
        first_mins = self.first_minutes[sessions]
        first_mins_plus_one = first_mins + self.ONE_MIN
        last_mins = self.last_minutes[sessions]
        last_mins_less_one = last_mins - self.ONE_MIN

        trading_mins = []
        break_mins = []

        for session, mins_ in zip(
            sessions,
            zip(first_mins, first_mins_plus_one, last_mins, last_mins_less_one),
        ):
            trading_mins.append((mins_, session))

        if self.has_a_break:
            last_am_mins = self.last_am_minutes[sessions]
            last_am_mins = last_am_mins[last_am_mins.notna()]
            first_pm_mins = self.first_pm_minutes[last_am_mins.index]

            last_am_mins_less_one = last_am_mins - self.ONE_MIN
            last_am_mins_plus_one = last_am_mins + self.ONE_MIN
            last_am_mins_plus_two = last_am_mins + self.TWO_MIN

            first_pm_mins_plus_one = first_pm_mins + self.ONE_MIN
            first_pm_mins_less_one = first_pm_mins - self.ONE_MIN
            first_pm_mins_less_two = first_pm_mins - self.TWO_MIN

            for session, mins_ in zip(
                last_am_mins.index,
                zip(
                    last_am_mins,
                    last_am_mins_less_one,
                    first_pm_mins,
                    first_pm_mins_plus_one,
                ),
            ):
                trading_mins.append((mins_, session))

            for session, mins_ in zip(
                last_am_mins.index,
                zip(
                    last_am_mins_plus_one,
                    last_am_mins_plus_two,
                    first_pm_mins_less_one,
                    first_pm_mins_less_two,
                ),
            ):
                break_mins.append((mins_, session))

        return (tuple(trading_mins), tuple(break_mins))

    @property
    def trading_minutes(self) -> tuple[tuple[tuple[pd.Timestamp], pd.Timestamp]]:
        """Sample of edge trading minutes.

        Returns
        -------
        tuple of tuple[tuple[trading_minutes], session]

            tuple[trading_minutes] includes:
                first two trading minutes of a session.
                last two trading minutes of a session.
                If breaks:
                    last two trading minutes of session's am subsession.
                    first two trading minutes of session's pm subsession.

            session
                Session of trading_minutes
        """
        return self._evaluate_trading_and_break_minutes()[0]

    def trading_minutes_only(self) -> abc.Iterator[pd.Timestamp]:
        """Generator of trading minutes of `self.trading_minutes`."""
        for mins, _ in self.trading_minutes:
            for minute in mins:
                yield minute

    @property
    def trading_minute(self) -> pd.Timestamp:
        """A single trading minute."""
        return self.trading_minutes[0][0][0]

    @property
    def break_minutes(self) -> tuple[tuple[tuple[pd.Timestamp], pd.Timestamp]]:
        """Sample of edge break minutes.

        Returns
        -------
        tuple of tuple[tuple[break_minutes], session]

            tuple[break_minutes]:
                first two minutes of a break.
                last two minutes of a break.

            session
                Session of break_minutes
        """
        return self._evaluate_trading_and_break_minutes()[1]

    def break_minutes_only(self) -> abc.Iterator[pd.Timestamp]:
        """Generator of break minutes of `self.break_minutes`."""
        for mins, _ in self.break_minutes:
            for minute in mins:
                yield minute

    # evaluted properties that are not sessions or trading minutes

    @functools.lru_cache(maxsize=4)
    def _non_trading_minutes(
        self,
    ) -> tuple[tuple[tuple[pd.Timestamp], pd.Timestamp, pd.Timestamp]]:
        non_trading_mins = []

        sessions = prev_sessions = self.get_sessions_sample(
            self.sessions_with_gap_after
        )
        next_sessions = self.sessions[self.sessions.get_indexer(sessions) + 1]

        last_mins_plus_one = self.last_minutes[sessions] + self.ONE_MIN
        first_mins_less_one = self.first_minutes[next_sessions] - self.ONE_MIN

        for prev_session, next_session, mins_ in zip(
            prev_sessions, next_sessions, zip(last_mins_plus_one, first_mins_less_one)
        ):
            non_trading_mins.append((mins_, prev_session, next_session))

        return tuple(non_trading_mins)

    @property
    def non_trading_minutes(
        self,
    ) -> tuple[tuple[tuple[pd.Timestamp], pd.Timestamp, pd.Timestamp]]:
        """Sample of edge non_trading_minutes. Does not include break minutes.

        Returns
        -------
        tuple of tuple[tuple[non-trading minute], previous session, next session]

            tuple[non-trading minute]
                Two non-trading minutes.
                    [0] first non-trading minute to follow a session.
                    [1] last non-trading minute prior to the next session.

            previous session
                Session that preceeds non-trading minutes.

            next session
                Session that follows non-trading minutes.

        See Also
        --------
        break_minutes
        """
        return self._non_trading_minutes()

    def non_trading_minutes_only(self) -> abc.Iterator[pd.Timestamp]:
        """Generator of non-trading minutes of `self.non_trading_minutes`."""
        for mins, _, _ in self.non_trading_minutes:
            for minute in mins:
                yield minute

    @property
    def non_sessions(self) -> pd.DatetimeIndex:
        """Dates (UTC midnight) within answers range that are not sessions."""
        all_dates = pd.date_range(
            start=self.first_session, end=self.last_session, freq="D"
        )
        return all_dates.difference(self.sessions)

    @property
    def sessions_range_defined_by_non_sessions(
        self,
    ) -> tuple[tuple[pd.Timestamp, pd.Timestamp], pd.Datetimeindex] | None:
        """Range containing sessions although defined with non-sessions.

        Returns
        -------
        tuple[tuple[pd.Timestamp, pd.Timestamp], pd.Datetimeindex]:
            [0] tuple[pd.Timestamp, pd.Timestamp]:
                [0] range start as non-session date.
                [1] range end as non-session date.
            [1] pd.DatetimeIndex:
                Sessions in range.
        """
        non_sessions = self.non_sessions
        if len(non_sessions) <= 1:
            return None
        limit = len(self.non_sessions) - 2
        i = 0
        start, end = non_sessions[i], non_sessions[i + 1]
        while (end - start) < pd.Timedelta(4, "D"):
            i += 1
            start, end = non_sessions[i], non_sessions[i + 1]
            if i == limit:
                # Unable to evaluate range from consecutive non-sessions
                # that covers >= 3 sessions. Just go with max range...
                start, end = non_sessions[0], non_sessions[-1]
        slice_start, slice_end = self.sessions.searchsorted((start, end))
        return (start, end), self.sessions[slice_start:slice_end]

    @property
    def non_sessions_run(self) -> pd.DatetimeIndex:
        """Longest run of non_sessions."""
        ser = self.sessions.to_series()
        diff = ser.shift(-1) - ser
        max_diff = diff.max()
        if max_diff == pd.Timedelta(1, "D"):
            return pd.DatetimeIndex([])
        session_before_run = diff[diff == max_diff].index[-1]
        run = pd.date_range(
            start=session_before_run + pd.Timedelta(1, "D"),
            periods=(max_diff // pd.Timedelta(1, "D")) - 1,
            freq="D",
        )
        assert run.isin(self.non_sessions).all()
        assert run[0] > self.first_session
        assert run[-1] < self.last_session
        return run

    @property
    def non_sessions_range(self) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """Longest range covering a period without a session."""
        non_sessions_run = self.non_sessions_run
        if non_sessions_run.empty:
            return None
        else:
            return self.non_sessions_run[0], self.non_sessions_run[-1]

    # method-specific inputs/outputs

    def prev_next_open_close_minutes(
        self,
    ) -> abc.Iterator[
        tuple[
            pd.Timestamp,
            tuple[
                pd.Timestamp | None,
                pd.Timestamp | None,
                pd.Timestamp | None,
                pd.Timestamp | None,
            ],
        ]
    ]:
        """Generator of test parameters for prev/next_open/close methods.

        Inputs include following minutes of each session:
            open
            one minute prior to open (not included for first session)
            one minute after open
            close
            one minute before close
            one minute after close (not included for last session)

        NB Assumed that minutes prior to first open and after last close
        will be handled via parse_timestamp.

        Yields
        ------
        2-tuple:
            [0] Input a minute sd pd.Timestamp
            [1] 4 tuple of expected output of corresponding method:
                [0] previous_open as pd.Timestamp | None
                [1] previous_close as pd.Timestamp | None
                [2] next_open as pd.Timestamp | None
                [3] next_close as pd.Timestamp | None

                NB None indicates that corresponding method is expected to
                raise a ValueError for this input.
        """
        close_is_next_open_bv = self.closes == self.opens.shift(-1)
        open_was_prev_close_bv = self.opens == self.closes.shift(+1)
        close_is_next_open = close_is_next_open_bv[0]

        # minutes for session 0
        minute = self.opens[0]
        yield (minute, (None, None, self.opens[1], self.closes[0]))

        minute = minute + self.ONE_MIN
        yield (minute, (self.opens[0], None, self.opens[1], self.closes[0]))

        minute = self.closes[0]
        next_open = self.opens[2] if close_is_next_open else self.opens[1]
        yield (minute, (self.opens[0], None, next_open, self.closes[1]))

        minute += self.ONE_MIN
        prev_open = self.opens[1] if close_is_next_open else self.opens[0]
        yield (minute, (prev_open, self.closes[0], next_open, self.closes[1]))

        minute = self.closes[0] - self.ONE_MIN
        yield (minute, (self.opens[0], None, self.opens[1], self.closes[0]))

        # minutes for sessions over [1:-1] except for -1 close and 'close + one_min'
        opens = self.opens[1:-1]
        closes = self.closes[1:-1]
        prev_opens = self.opens[:-2]
        prev_closes = self.closes[:-2]
        next_opens = self.opens[2:]
        next_closes = self.closes[2:]
        opens_after_next = self.opens[3:]
        # add dummy row to equal lengths (won't be used)
        _ = pd.Series(pd.Timestamp("2200-01-01", tz="UTC"))
        opens_after_next = opens_after_next.append(_)

        stop = closes[-1]

        for (
            open_,
            close,
            prev_open,
            prev_close,
            next_open,
            next_close,
            open_after_next,
            close_is_next_open,
            open_was_prev_close,
        ) in zip(
            opens,
            closes,
            prev_opens,
            prev_closes,
            next_opens,
            next_closes,
            opens_after_next,
            close_is_next_open_bv[1:-2],
            open_was_prev_close_bv[1:-2],
        ):
            if not open_was_prev_close:
                # only include open minutes if not otherwise duplicating
                # evaluations already made for prior close.
                yield (open_, (prev_open, prev_close, next_open, close))
                yield (open_ - self.ONE_MIN, (prev_open, prev_close, open_, close))
                yield (open_ + self.ONE_MIN, (open_, prev_close, next_open, close))

            yield (close - self.ONE_MIN, (open_, prev_close, next_open, close))

            if close != stop:
                next_open_ = open_after_next if close_is_next_open else next_open
                yield (close, (open_, prev_close, next_open_, next_close))

                open_ = next_open if close_is_next_open else open_
                yield (close + self.ONE_MIN, (open_, close, next_open_, next_close))

        # close and 'close + one_min' for session -2
        minute = self.closes[-2]
        next_open = None if close_is_next_open_bv[-2] else self.opens[-1]
        yield (minute, (self.opens[-2], self.closes[-3], next_open, self.closes[-1]))

        minute += self.ONE_MIN
        prev_open = self.opens[-1] if close_is_next_open_bv[-2] else self.opens[-2]
        yield (minute, (prev_open, self.closes[-2], next_open, self.closes[-1]))

        # minutes for session -1
        if not open_was_prev_close_bv[-1]:
            open_ = self.opens[-1]
            prev_open = self.opens[-2]
            prev_close = self.closes[-2]
            next_open = None
            close = self.closes[-1]
            yield (open_, (prev_open, prev_close, next_open, close))
            yield (open_ - self.ONE_MIN, (prev_open, prev_close, open_, close))
            yield (open_ + self.ONE_MIN, (open_, prev_close, next_open, close))

        minute = self.closes[-1]
        next_open = self.opens[2] if close_is_next_open_bv[-1] else self.opens[1]
        yield (minute, (self.opens[-1], self.closes[-2], None, None))

        minute -= self.ONE_MIN
        yield (minute, (self.opens[-1], self.closes[-2], None, self.closes[-1]))

    # out-of-bounds properties

    @property
    def minute_too_early(self) -> pd.Timestamp:
        """Minute earlier than first trading minute."""
        return self.first_trading_minute - self.ONE_MIN

    @property
    def minute_too_late(self) -> pd.Timestamp:
        """Minute later than last trading minute."""
        return self.last_trading_minute + self.ONE_MIN

    @property
    def session_too_early(self) -> pd.Timestamp:
        """Date earlier than first session."""
        return self.first_session - self.ONE_DAY

    @property
    def session_too_late(self) -> pd.Timestamp:
        """Date later than last session."""
        return self.last_session + self.ONE_DAY

    # dunder

    def __repr__(self) -> str:
        return f"<Answers: calendar {self.name}, side {self.side}>"


def no_parsing(f: typing.Callable):
    """Wrap a method under test so that it skips input parsing."""
    return lambda *args, **kwargs: f(*args, _parse=False, **kwargs)


class ExchangeCalendarTestBaseProposal:
    """Test base for an ExchangeCalendar.

    Notes
    -----

    ---Fixtures---

    In accordance with the pytest framework, whilst methods are requried to
    have `self` as their first argument, no method should use `self`.
    All required inputs should come by way of including fixtures to a
    test method's arguments.

    Methods that are directly or indirectly dependent on the evaluation of
    trading minutes should be tested against the parameterized
    all_calendars_with_answers fixture. This fixture will execute the test
    against multiple calendar instances, one for each viable `side`.

    The following methods directly evaluate trading minutes:
        all_minutes
        _last_minute_nanos()
        _last_am_minute_nanos()
        _first_minute_nanos()
        _first_pm_minute_nanos()
    NB this list does not include methods that indirectly evaluate methods
    by way of calling (directly or indirectly) one of the above methods.

    Methods that are not dependent on the evaluation of trading minutes
    should be tested against only the default_calendar_with_answers or
    default_calendar fixture.

    Calendar instances provided by fixtures should be used exclusively to
    call the method being tested. NO TEST INPUT OR EXPECTED OUTPUT SHOULD
    BE EVALUATED BY WAY OF CALLING A CALENDAR METHOD. Rather, test
    inputs and expected output should be taken directly, or evaluated from,
    properties/methods of the corresponding Answers fixture.
    """

    # subclass must override the following fixtures

    @pytest.fixture(scope="class")
    def calendar_cls(self) -> abc.Iterator[ExchangeCalendar]:
        """ExchangeCalendar class to be tested.

        Examples:
            XNYSExchangeCalendar
            AlwaysOpenCalendar
        """
        raise NotImplementedError("fixture must be implemented on subclass")

    @pytest.fixture(scope="class")
    def max_session_hours(self) -> abc.Iterator[int | float]:
        """Largest number of hours that can comprise a single session.

        Examples:
            8
            6.5
        """
        raise NotImplementedError("fixture must be implemented on subclass")

    # if subclass has a 24h session then subclass must override this fixture,
    # defining on subclass as is here, only difference being list passed to
    # decorator's 'params' arg should be ["left", "right"].
    @pytest.fixture(scope="class", params=["both", "left", "right", "neither"])
    def all_calendars_with_answers(
        self, request, calendars, answers
    ) -> abc.Iterator[tuple[ExchangeCalendar, Answers]]:
        """Parameterized calendars and answers for each side."""
        yield (calendars[request.param], answers[request.param])

    # subclass should override the following fixtures in the event that the
    # default defined here does not apply.

    @pytest.fixture(scope="class")
    def start_bound(self) -> abc.Iterator[pd.Timestamp | None]:
        """Earliest date for which calendar can be instantiated, or None if
        there is no start bound."""
        yield None

    @pytest.fixture(scope="class")
    def end_bound(self) -> abc.Iterator[pd.Timestamp | None]:
        """Latest date for which calendar can be instantiated, or None if
        there is no end bound."""
        yield None

    # base class fixtures

    @pytest.fixture(scope="class")
    def name(self, calendar_cls) -> abc.Iterator[str]:
        """Calendar name."""
        yield calendar_cls.name

    @pytest.fixture(scope="class")
    def has_24h_session(self, name) -> abc.Iterator[bool]:
        df = get_csv(name)
        yield (df.market_close == df.market_open.shift(-1)).any()

    @pytest.fixture(scope="class")
    def default_side(self, has_24h_session) -> abc.Iterator[str]:
        """Default calendar side."""
        if has_24h_session:
            yield "left"
        else:
            yield "both"

    @pytest.fixture(scope="class")
    def sides(self, has_24h_session) -> abc.Iterator[list[str]]:
        """All valid sides options for calendar."""
        if has_24h_session:
            yield ["left", "right"]
        else:
            yield ["both", "left", "right", "neither"]

    # calendars and answers

    @pytest.fixture(scope="class")
    def answers(self, name, sides) -> abc.Iterator[dict[str, Answers]]:
        """Dict of answers, key as side, value as corresoponding answers."""
        yield {side: Answers(name, side) for side in sides}

    @pytest.fixture(scope="class")
    def default_answers(self, answers, default_side) -> abc.Iterator[Answers]:
        yield answers[default_side]

    @pytest.fixture(scope="class")
    def calendars(
        self, calendar_cls, default_answers, sides
    ) -> abc.Iterator[dict[str, ExchangeCalendar]]:
        """Dict of calendars, key as side, value as corresoponding calendar."""
        start = default_answers.first_session
        end = default_answers.last_session
        yield {side: calendar_cls(start, end, side) for side in sides}

    @pytest.fixture(scope="class")
    def default_calendar(
        self, calendars, default_side
    ) -> abc.Iterator[ExchangeCalendar]:
        yield calendars[default_side]

    @pytest.fixture(scope="class")
    def calendars_with_answers(
        self, calendars, answers, sides
    ) -> abc.Iterator[dict[str, tuple[ExchangeCalendar, Answers]]]:
        """Dict of calendars and answers, key as side."""
        yield {side: (calendars[side], answers[side]) for side in sides}

    @pytest.fixture(scope="class")
    def default_calendar_with_answers(
        self, calendars_with_answers, default_side
    ) -> abc.Iterator[tuple[ExchangeCalendar, Answers]]:
        yield calendars_with_answers[default_side]

    # general use fixtures. Subclass should NOT override.

    @pytest.fixture(scope="class")
    def one_minute(self) -> abc.Iterator[pd.Timedelta]:
        yield pd.Timedelta(1, "T")

    @pytest.fixture(scope="class")
    def today(self) -> abc.Iterator[pd.Timedelta]:
        yield pd.Timestamp.now(tz="UTC").floor("D")

    @pytest.fixture(scope="class", params=["next", "previous", "none"])
    def all_directions(self, request) -> abc.Iterator[str]:
        """Parameterised fixture of direction to go if minute is not a trading minute"""
        yield request.param

    # TESTS

    # Tests for calendar definition and construction methods.

    def test_calculated_against_csv(self, default_calendar_with_answers):
        calendar, ans = default_calendar_with_answers
        tm.assert_index_equal(calendar.schedule.index, ans.sessions)

    def test_bound_start(self, calendar_cls, start_bound, today):
        if start_bound is not None:
            cal = calendar_cls(start_bound, today)
            assert isinstance(cal, ExchangeCalendar)

            start = start_bound - pd.DateOffset(days=1)
            with pytest.raises(ValueError, match=re.escape(f"{start}")):
                calendar_cls(start, today)
        else:
            # verify no bound imposed
            cal = calendar_cls(pd.Timestamp("1902-01-01", tz="UTC"), today)
            assert isinstance(cal, ExchangeCalendar)

    def test_bound_end(self, calendar_cls, end_bound, today):
        if end_bound is not None:
            cal = calendar_cls(today, end_bound)
            assert isinstance(cal, ExchangeCalendar)

            end = end_bound + pd.DateOffset(days=1)
            with pytest.raises(ValueError, match=re.escape(f"{end}")):
                calendar_cls(today, end)
        else:
            # verify no bound imposed
            cal = calendar_cls(today, pd.Timestamp("2050-01-01", tz="UTC"))
            assert isinstance(cal, ExchangeCalendar)

    def test_sanity_check_session_lengths(self, default_calendar, max_session_hours):
        cal = default_calendar
        cal_max_secs = (cal.market_closes_nanos - cal.market_opens_nanos).max()
        assert cal_max_secs / 3600000000000 <= max_session_hours

    def test_adhoc_holidays_specification(self, default_calendar):
        """adhoc holidays should be tz-naive (#33, #39)."""
        dti = pd.DatetimeIndex(default_calendar.adhoc_holidays)
        assert dti.tz is None

    def test_invalid_input(self, calendar_cls, sides, default_answers, name):
        ans = default_answers

        invalid_side = "both" if "both" not in sides else "invalid_side"
        error_msg = f"`side` must be in {sides} although received as {invalid_side}."
        with pytest.raises(ValueError, match=re.escape(error_msg)):
            calendar_cls(side=invalid_side)

        start = ans.sessions[1]
        end_same_as_start = ans.sessions[1]
        error_msg = (
            "`start` must be earlier than `end` although `start` parsed as"
            f" '{start}' and `end` as '{end_same_as_start}'."
        )
        with pytest.raises(ValueError, match=re.escape(error_msg)):
            calendar_cls(start=start, end=end_same_as_start)

        end_before_start = ans.sessions[0]
        error_msg = (
            "`start` must be earlier than `end` although `start` parsed as"
            f" '{start}' and `end` as '{end_before_start}'."
        )
        with pytest.raises(ValueError, match=re.escape(error_msg)):
            calendar_cls(start=start, end=end_before_start)

        non_sessions = ans.non_sessions_run
        if not non_sessions.empty:
            start = non_sessions[0]
            end = non_sessions[-1]
            error_msg = (
                f"The requested ExchangeCalendar, {name.upper()}, cannot be created as"
                f" there would be no sessions between the requested `start` ('{start}')"
                f" and `end` ('{end}') dates."
            )
            with pytest.raises(NoSessionsError, match=re.escape(error_msg)):
                calendar_cls(start=start, end=end)

    # Tests for properties covering all sessions.

    def test_minutes_properties(self, all_calendars_with_answers):
        """Test minute properties.

        Tests following calendar properties:
            all_first_minutes
            all_last_minutes
            all_last_am_minutes
            all_first_pm_minutes
        """
        cal, ans = all_calendars_with_answers

        for prop in (
            "first_minutes",
            "last_minutes",
            "last_am_minutes",
            "first_pm_minutes",
        ):
            ans_minutes = getattr(ans, prop).dt.tz_convert(None)
            cal_minutes = getattr(cal, "all_" + prop)
            tm.assert_series_equal(ans_minutes, cal_minutes, check_freq=False)

    # Tests for properties covering all minutes.

    def test_all_minutes(self, all_calendars_with_answers, one_minute):
        """Test trading minutes at sessions' bounds."""
        calendar, ans = all_calendars_with_answers

        side = ans.side
        mins = calendar.all_minutes
        assert isinstance(mins, pd.DatetimeIndex)
        assert not mins.empty
        mins_plus_1 = mins + one_minute
        mins_less_1 = mins - one_minute

        if side in ["left", "neither"]:
            # Test that close and break_start not in mins,
            # but are in mins_plus_1 (unless no gap after)

            # do not test here for sessions with no gap after as for "left" these
            # sessions' close IS a trading minute as it's the same as next session's
            # open.
            # NB For "neither" all sessions will have gap after.
            closes = ans.closes[ans.sessions_with_gap_after]
            # closes should not be in minutes
            assert not mins.isin(closes).any()
            # all closes should be in minutes plus 1
            # for speed, use only subset of mins that are of interest
            mins_plus_1_on_close = mins_plus_1[mins_plus_1.isin(closes)]
            assert closes.isin(mins_plus_1_on_close).all()

            # as noted above, if no gap after then close should be a trading minute
            # as will be first minute of next session.
            closes = ans.closes[ans.sessions_without_gap_after]
            mins_on_close = mins[mins.isin(closes)]
            assert closes.isin(mins_on_close).all()

            if ans.has_a_break:
                # break start should not be in minutes
                assert not mins.isin(ans.break_starts).any()
                # break start should be in minutes plus 1
                break_starts = ans.break_starts[ans.sessions_with_break]
                mins_plus_1_on_start = mins_plus_1[mins_plus_1.isin(break_starts)]
                assert break_starts.isin(mins_plus_1_on_start).all()

        if side in ["left", "both"]:
            # Test that open and break_end are in mins,
            # but not in mins_plus_1 (unless no gap before)
            mins_on_open = mins[mins.isin(ans.opens)]
            assert ans.opens.isin(mins_on_open).all()

            opens = ans.opens[ans.sessions_with_gap_before]
            assert not mins_plus_1.isin(opens).any()

            opens = ans.opens[ans.sessions_without_gap_before]
            mins_plus_1_on_open = mins_plus_1[mins_plus_1.isin(opens)]
            assert opens.isin(mins_plus_1_on_open).all()

            if ans.has_a_break:
                break_ends = ans.break_ends[ans.sessions_with_break]
                mins_on_end = mins[mins.isin(ans.break_ends)]
                assert break_ends.isin(mins_on_end).all()

        if side in ["right", "neither"]:
            # Test that open and break_end are not in mins,
            # but are in mins_less_1 (unless no gap before)
            opens = ans.opens[ans.sessions_with_gap_before]
            assert not mins.isin(opens).any()

            mins_less_1_on_open = mins_less_1[mins_less_1.isin(opens)]
            assert opens.isin(mins_less_1_on_open).all()

            opens = ans.opens[ans.sessions_without_gap_before]
            mins_on_open = mins[mins.isin(opens)]
            assert opens.isin(mins_on_open).all()

            if ans.has_a_break:
                assert not mins.isin(ans.break_ends).any()
                break_ends = ans.break_ends[ans.sessions_with_break]
                mins_less_1_on_end = mins_less_1[mins_less_1.isin(break_ends)]
                assert break_ends.isin(mins_less_1_on_end).all()

        if side in ["right", "both"]:
            # Test that close and break_start are in mins,
            # but not in mins_less_1 (unless no gap after)
            mins_on_close = mins[mins.isin(ans.closes)]
            assert ans.closes.isin(mins_on_close).all()

            closes = ans.closes[ans.sessions_with_gap_after]
            assert not mins_less_1.isin(closes).any()

            closes = ans.closes[ans.sessions_without_gap_after]
            mins_less_1_on_close = mins_less_1[mins_less_1.isin(closes)]
            assert closes.isin(mins_less_1_on_close).all()

            if ans.has_a_break:
                break_starts = ans.break_starts[ans.sessions_with_break]
                mins_on_start = mins[mins.isin(ans.break_starts)]
                assert break_starts.isin(mins_on_start).all()

    # Tests for calendar properties.

    # Tests for methods that interrogate a given session.

    def test_session_open_close_break_start_end(self, default_calendar_with_answers):
        """Test methods that get session open, close, break_start, break_end.

        Tests following calendar methods:
            session_open
            session_close
            open_and_close_for_session
            session_break_start
            session_break_end
            break_start_and_end_for_session
        """
        # considered sufficient to limit test to sessions of session blocks.
        cal, ans = default_calendar_with_answers
        for _, block in ans.session_block_generator():
            for session in block:
                ans_open = ans.opens[session]
                ans_close = ans.closes[session]
                assert cal.session_open(session, _parse=False) == ans_open
                assert cal.session_close(session, _parse=False) == ans_close
                assert cal.open_and_close_for_session(session, _parse=False) == (
                    ans_open,
                    ans_close,
                )

                break_start = cal.session_break_start(session, _parse=False)
                break_end = cal.session_break_end(session, _parse=False)
                break_start_and_end = cal.break_start_and_end_for_session(
                    session, _parse=False
                )
                ans_break_start = ans.break_starts[session]
                ans_break_end = ans.break_ends[session]
                if pd.isna(ans_break_start):
                    assert pd.isna(break_start) and pd.isna(break_end)
                    assert pd.isna(break_start_and_end[0])
                    assert pd.isna(break_start_and_end[1])
                else:
                    assert break_start == ans_break_start
                    assert break_end == ans_break_end
                    assert break_start_and_end[0] == ans_break_start
                    assert break_start_and_end[1] == ans_break_end

    def test_session_minute_methods(self, all_calendars_with_answers):
        """Test methods that get a minute bound of a session or subsession.

        Tests following calendar methods:
            session_first_minute
            session_last_minute
            session_last_am_minute
            session_first_pm_minute
            session_first_and_last_minute
        """
        # considered sufficient to limit test to sessions of session blocks.
        cal, ans = all_calendars_with_answers
        for _, block in ans.session_block_generator():
            for session in block:
                ans_first_minute = ans.first_minutes[session]
                ans_last_minute = ans.last_minutes[session]
                assert (
                    cal.session_first_minute(session, _parse=False) == ans_first_minute
                )
                assert cal.session_last_minute(session, _parse=False) == ans_last_minute
                assert cal.session_first_and_last_minute(session, _parse=False) == (
                    ans_first_minute,
                    ans_last_minute,
                )

                last_am_minute = cal.session_last_am_minute(session, _parse=False)
                first_pm_minute = cal.session_first_pm_minute(session, _parse=False)
                ans_last_am_minute = ans.last_am_minutes[session]
                ans_first_pm_minute = ans.first_pm_minutes[session]
                if pd.isna(ans_last_am_minute):
                    assert pd.isna(last_am_minute) and pd.isna(first_pm_minute)
                else:
                    assert last_am_minute == ans_last_am_minute
                    assert first_pm_minute == ans_first_pm_minute

    def test_next_prev_session(self, default_calendar_with_answers):
        cal, ans = default_calendar_with_answers
        m_prev = no_parsing(cal.previous_session_label)
        m_next = no_parsing(cal.next_session_label)

        # NB non-sessions handled by methods via parse_session

        # first session
        with pytest.raises(ValueError):
            m_prev(ans.first_session)

        # middle sessions (and m_prev for last session)
        for session, next_session in zip(ans.sessions[:-1], ans.sessions[1:]):
            assert m_next(session) == next_session
            assert m_prev(next_session) == session

        # last session
        with pytest.raises(ValueError):
            m_next(ans.last_session)

    def test_minutes_for_session(self, all_calendars_with_answers):
        cal, ans = all_calendars_with_answers
        m = no_parsing(cal.minutes_for_session)

        # Limit test to every session of each session block.

        for _, block in ans.session_block_generator():
            for session in block:
                tm.assert_index_equal(m(session), ans.get_sessions_minutes(session))

    # Tests for methods that interrogate a date.

    def test_date_to_session_label(self, default_calendar_with_answers):
        cal, ans = default_calendar_with_answers
        m = no_parsing(cal.date_to_session_label)

        # test for error if request session prior to first calendar session.
        error_msg = (
            "Cannot get a session label prior to the first calendar"
            f" session ('{ans.first_session}'). Consider passing"
            " `direction` as 'next'."
        )
        with pytest.raises(ValueError, match=re.escape(error_msg)):
            m(ans.session_too_early, "previous")

        sessions = ans.sessions

        # direction as "previous"
        dates = pd.date_range(sessions[0], sessions[-1], freq="D")
        date_is_session = dates.isin(sessions)

        last_session = None
        for date, is_session in zip(dates, date_is_session):
            session_label = m(date, "previous")
            if is_session:
                assert session_label == date
                last_session = session_label
            else:
                assert session_label == last_session

        # direction as "next"
        last_session = None
        for date, is_session in zip(
            dates.sort_values(ascending=False), date_is_session[::-1]
        ):
            session_label = m(date, "next")
            if date in sessions:
                assert session_label == date
                last_session = session_label
            else:
                assert session_label == last_session

        # test for error if request session after last calendar session.
        error_msg = (
            "Cannot get a session label later than the last calendar"
            f" session ('{ans.last_session}'). Consider passing"
            " `direction` as 'previous'."
        )
        with pytest.raises(ValueError, match=re.escape(error_msg)):
            m(ans.session_too_late, "next")

        # test for non_sessions without direction
        if not ans.non_sessions.empty:
            for non_session in ans.non_sessions[0 : None : len(ans.non_sessions) // 9]:
                error_msg = (
                    f"`date` '{non_session}' is not a session label. Consider"
                    " passing a `direction`."
                )
                with pytest.raises(ValueError, match=re.escape(error_msg)):
                    m(non_session, "none")
                # test default behaviour
                with pytest.raises(ValueError, match=re.escape(error_msg)):
                    m(non_session)

            # non-valid direction (only raised if pass a date that is not a session)
            error_msg = (
                "'not a direction' is not a valid `direction`. Valid `direction`"
                ' values are "next", "previous" and "none".'
            )
            with pytest.raises(ValueError, match=re.escape(error_msg)):
                m(non_session, "not a direction")

    # Tests for methods that interrogate a given minute (trading or non-trading)

    def test_is_trading_minute(self, all_calendars_with_answers):
        calendar, ans = all_calendars_with_answers
        m = no_parsing(calendar.is_trading_minute)

        for non_trading_min in ans.non_trading_minutes_only():
            assert m(non_trading_min) is False

        for trading_min in ans.trading_minutes_only():
            assert m(trading_min) is True

        for break_min in ans.break_minutes_only():
            assert m(break_min) is False

    def test_is_break_minute(self, all_calendars_with_answers):
        calendar, ans = all_calendars_with_answers
        m = no_parsing(calendar.is_break_minute)

        for non_trading_min in islice(ans.non_trading_minutes_only(), 0, None, 59):
            # limit testing to every 59th as non_trading minutes not edge cases
            assert m(non_trading_min) is False

        for trading_min in ans.trading_minutes_only():
            assert m(trading_min) is False

        for break_min in ans.break_minutes_only():
            assert m(break_min) is True

    def test_is_open_on_minute(self, all_calendars_with_answers):
        calendar, ans = all_calendars_with_answers
        m = no_parsing(calendar.is_open_on_minute)

        # minimal test as is_open_on_minute delegates evaluation to is_trading_minute
        # and is_break_minute, both of which are comprehensively tested.

        for non_trading_min in islice(ans.non_trading_minutes_only(), 50):
            assert m(non_trading_min) is False

        for trading_min in islice(ans.trading_minutes_only(), 50):
            assert m(trading_min) is True

        for break_min in islice(ans.break_minutes_only(), 1000):
            rtrn = m(break_min, ignore_breaks=True)
            assert rtrn is True
            rtrn = m(break_min)
            assert rtrn is False

    def test_prev_next_open_close(self, default_calendar_with_answers):
        """Test methods that return previous/next open/close.

        Tests methods:
            previous_open
            previous_close
            next_open
            next_close
        """
        cal, ans = default_calendar_with_answers
        generator = ans.prev_next_open_close_minutes()

        for minute, (prev_open, prev_close, next_open, next_close) in generator:
            if prev_open is None:
                with pytest.raises(ValueError):
                    cal.previous_open(minute, _parse=False)
            else:
                assert cal.previous_open(minute, _parse=False) == prev_open

            if prev_close is None:
                with pytest.raises(ValueError):
                    cal.previous_close(minute, _parse=False)
            else:
                assert cal.previous_close(minute, _parse=False) == prev_close

            if next_open is None:
                with pytest.raises(ValueError):
                    cal.next_open(minute, _parse=False)
            else:
                assert cal.next_open(minute, _parse=False) == next_open

            if next_close is None:
                with pytest.raises(ValueError):
                    cal.next_close(minute, _parse=False)
            else:
                assert cal.next_close(minute, _parse=False) == next_close

    def test_prev_next_minute(self, all_calendars_with_answers, one_minute):
        """Test methods that return previous/next minute.

        Tests methods:
            next_minute
            previous_minute
        """
        cal, ans = all_calendars_with_answers
        next_m = no_parsing(cal.next_minute)
        prev_m = no_parsing(cal.previous_minute)

        # minutes of first session
        first_min = ans.first_minutes[0]
        first_min_plus_one = ans.first_minutes_plus_one[0]
        first_min_less_one = ans.first_minutes_less_one[0]
        last_min = ans.last_minutes[0]
        last_min_plus_one = ans.last_minutes_plus_one[0]
        last_min_less_one = ans.last_minutes_less_one[0]

        with pytest.raises(ValueError):
            prev_m(first_min)
        # minutes earlier than first_trading_minute assumed handled via parse_timestamp
        assert next_m(first_min) == first_min_plus_one
        assert next_m(first_min_plus_one) == first_min_plus_one + one_minute
        assert prev_m(first_min_plus_one) == first_min
        assert prev_m(last_min) == last_min_less_one
        assert prev_m(last_min_less_one) == last_min_less_one - one_minute
        assert next_m(last_min_less_one) == last_min
        assert prev_m(last_min_plus_one) == last_min

        prev_last_min = last_min
        for (
            first_min,
            first_min_plus_one,
            first_min_less_one,
            last_min,
            last_min_plus_one,
            last_min_less_one,
            gap_before,
        ) in zip(
            ans.first_minutes[1:],
            ans.first_minutes_plus_one[1:],
            ans.first_minutes_less_one[1:],
            ans.last_minutes[1:],
            ans.last_minutes_plus_one[1:],
            ans.last_minutes_less_one[1:],
            ~ans._mask_sessions_without_gap_before[1:],
        ):
            assert next_m(prev_last_min) == first_min
            assert prev_m(first_min) == prev_last_min
            assert next_m(first_min) == first_min_plus_one
            assert prev_m(first_min_plus_one) == first_min
            assert next_m(first_min_less_one) == first_min
            assert prev_m(last_min) == last_min_less_one
            assert next_m(last_min_less_one) == last_min
            assert prev_m(last_min_plus_one) == last_min

            if gap_before:
                assert next_m(prev_last_min + one_minute) == first_min
                assert prev_m(first_min_less_one) == prev_last_min
            else:
                assert next_m(prev_last_min + one_minute) == first_min_plus_one
                assert next_m(prev_last_min + one_minute) == first_min_plus_one

            prev_last_min = last_min

        with pytest.raises(ValueError):
            next_m(last_min)
        # minutes later than last_trading_minute assumed handled via parse_timestamp

        if ans.has_a_break:
            for (
                last_am_min,
                last_am_min_less_one,
                last_am_min_plus_one,
                first_pm_min,
                first_pm_min_less_one,
                first_pm_min_plus_one,
            ) in zip(
                ans.last_am_minutes,
                ans.last_am_minutes_less_one,
                ans.last_am_minutes_plus_one,
                ans.first_pm_minutes,
                ans.first_pm_minutes_less_one,
                ans.first_pm_minutes_plus_one,
            ):
                if pd.isna(last_am_min):
                    continue
                assert next_m(last_am_min_less_one) == last_am_min
                assert next_m(last_am_min) == first_pm_min
                assert prev_m(last_am_min) == last_am_min_less_one
                assert next_m(last_am_min_plus_one) == first_pm_min
                assert prev_m(last_am_min_plus_one) == last_am_min

                assert prev_m(first_pm_min_less_one) == last_am_min
                assert next_m(first_pm_min_less_one) == first_pm_min
                assert prev_m(first_pm_min) == last_am_min
                assert next_m(first_pm_min) == first_pm_min_plus_one
                assert prev_m(first_pm_min_plus_one) == first_pm_min

    def test_minute_to_session_label(self, all_calendars_with_answers, all_directions):
        direction = all_directions
        calendar, ans = all_calendars_with_answers
        m = no_parsing(calendar.minute_to_session_label)

        for non_trading_mins, prev_session, next_session in ans.non_trading_minutes:
            for non_trading_min in non_trading_mins:
                if direction == "none":
                    with pytest.raises(ValueError):
                        m(non_trading_min, direction)
                else:
                    session = m(non_trading_min, direction)
                    if direction == "next":
                        assert session == next_session
                    else:
                        assert session == prev_session

        for trading_minutes, session in ans.trading_minutes:
            for trading_minute in trading_minutes:
                rtrn = m(trading_minute, direction)
                assert rtrn == session

        if ans.has_a_break:
            for i, (break_minutes, session) in enumerate(ans.break_minutes):
                if i == 15:
                    break
                for break_minute in break_minutes:
                    rtrn = m(break_minute, direction)
                    assert rtrn == session

        oob_minute = ans.minute_too_early
        if direction in ["previous", "none"]:
            error_msg = (
                f"Received `minute` as '{oob_minute}' although this is earlier than"
                f" the calendar's first trading minute ({ans.first_trading_minute})"
            )
            with pytest.raises(ValueError, match=re.escape(error_msg)):
                m(oob_minute, direction)
        else:
            session = m(oob_minute, direction)
            assert session == ans.first_session

        oob_minute = ans.minute_too_late
        if direction in ["next", "none"]:
            error_msg = (
                f"Received `minute` as '{oob_minute}' although this is later"
                f" than the calendar's last trading minute ({ans.last_trading_minute})"
            )
            with pytest.raises(ValueError, match=re.escape(error_msg)):
                m(oob_minute, direction)
        else:
            session = m(oob_minute, direction)
            assert session == ans.last_session

    # Tests for methods that evaluate or interrogate a range of minutes.

    def test_minutes_in_range(self, all_calendars_with_answers, one_minute):
        cal, ans = all_calendars_with_answers
        m = no_parsing(cal.minutes_in_range)

        block_minutes = ans.session_block_minutes
        for name, block in ans.session_block_generator():
            ans_dti = block_minutes[name]
            from_ = ans.first_minutes[block][0]
            to = ans.last_minutes[block[-1]]
            cal_dti = m(from_, to)
            tm.assert_index_equal(ans_dti, cal_dti)

            # test consequence of getting range from one minute before/after the
            # block's first/last trading minute.
            if name in ["first_three", "last_three"]:
                continue
            cal_dti = m(from_ - one_minute, to + one_minute)
            start_idx = 1 if block[0] in ans.sessions_without_gap_before else 0
            end_idx = -1 if block[-1] in ans.sessions_without_gap_after else None
            tm.assert_index_equal(ans_dti, cal_dti[start_idx:end_idx])

        # intra-session
        from_ = ans.first_minutes[ans.first_session] + pd.Timedelta(15, "T")
        to = ans.first_minutes[ans.first_session] + pd.Timedelta(45, "T")
        expected = pd.date_range(from_, to, freq="T")
        rtrn = m(from_, to)
        tm.assert_index_equal(expected, rtrn)

        # inter-session
        if not ans.sessions_with_gap_after.empty:
            session = ans.sessions_with_gap_after[0]
            next_session = ans.get_next_session(session)
            from_ = ans.last_minutes[session] + one_minute
            to = ans.first_minutes[next_session] - one_minute
            assert m(from_, to).empty

    def test_minute_index_to_session_labels(self, all_calendars_with_answers):
        calendar, ans = all_calendars_with_answers
        m = calendar.minute_index_to_session_labels

        trading_minute = ans.trading_minute
        for minute in islice(ans.non_trading_minutes_only(), 300):
            with pytest.raises(ValueError):
                m(pd.DatetimeIndex([minute]))
            with pytest.raises(ValueError):
                m(pd.DatetimeIndex([trading_minute, minute]))

        mins, sessions = [], []
        for trading_minutes, session in ans.trading_minutes[:30]:
            mins.extend(trading_minutes)
            sessions.extend([session] * len(trading_minutes))

        index = pd.DatetimeIndex(mins).sort_values()
        sessions_labels = m(index)
        assert sessions_labels.equals(pd.DatetimeIndex(sessions).sort_values())

    # Tests for methods that evaluate or interrogate a range of sessions.

    def test_sessions_in_range(self, default_calendar_with_answers):
        cal, ans = default_calendar_with_answers
        m = no_parsing(cal.sessions_in_range)

        # test where start and end are sessions
        start, end = ans.sessions[10], ans.sessions[-10]
        tm.assert_index_equal(m(start, end), ans.sessions[10:-9])

        # test session blocks
        for _, block in ans.session_block_generator():
            tm.assert_index_equal(m(block[0], block[-1]), block)

        # tests where start and end are non-session dates
        if len(ans.non_sessions) > 1:
            # test that range within which there are no sessions returns empty
            assert m(*ans.non_sessions_range).empty

            # test range defined with start and end as non-sessions
            (start, end), sessions = ans.sessions_range_defined_by_non_sessions
            tm.assert_index_equal(m(start, end), sessions)

    def test_sessions_window(self, default_calendar_with_answers):
        cal, ans = default_calendar_with_answers
        m = no_parsing(cal.sessions_window)

        for _, block in ans.session_block_generator():
            count = len(block) - 1
            tm.assert_index_equal(m(block[0], count), block)
            tm.assert_index_equal(m(block[-1], -count), block)

        # window starts on first calendar session
        assert m(ans.sessions[2], count=-2)[0] == ans.first_session
        # window would start before first calendar session
        with pytest.raises(ValueError):
            m(ans.sessions[2], count=-3)

        # window ends on last calendar session
        assert m(ans.sessions[-3], count=2)[-1] == ans.last_session
        # window would end after last calendar session
        with pytest.raises(ValueError):
            m(ans.sessions[-3], count=3)

    def test_session_distance(self, default_calendar_with_answers):
        cal, ans = default_calendar_with_answers
        m = no_parsing(cal.session_distance)

        for _, block in ans.session_block_generator():
            distance = len(block)
            assert m(block[0], block[-1]) == distance
            assert m(block[-1], block[0]) == -distance

        # test for same start / end
        assert m(ans.sessions[0], ans.sessions[0]) == 1

        # tests where start and end are non-session dates
        if len(ans.non_sessions) > 1:
            # test that range within which there are no sessions returns 0
            assert m(*ans.non_sessions_range) == 0

            # test range defined with start and end as non_sessions
            (start, end), sessions = ans.sessions_range_defined_by_non_sessions
            assert m(start, end) == len(sessions)

    def test_minutes_for_sessions_in_range(self, all_calendars_with_answers):
        cal, ans = all_calendars_with_answers
        m = no_parsing(cal.minutes_for_sessions_in_range)

        block_minutes = ans.session_block_minutes
        for name, block in ans.session_block_generator():
            ans_minutes = block_minutes[name]
            cal_minutes = m(block[0], block[-1])
            tm.assert_index_equal(ans_minutes, cal_minutes)

        # tests where start and end are non-session dates
        if len(ans.non_sessions) > 1:
            # test that range within which there are no sessions returns empty
            assert m(*ans.non_sessions_range).empty

            # test range defined with start and end as non-sessions
            (start, end), sessions = ans.sessions_range_defined_by_non_sessions
            minutes = ans.get_sessions_minutes(sessions[0], sessions[-1])
            tm.assert_index_equal(m(start, end), minutes)

    def test_session_opens_closes_in_range(self, default_calendar_with_answers):
        """Test methods that return range of open / close times.

        Tests methods:
            session_opens_in_range
            session_closes_in_range
        """
        cal, ans = default_calendar_with_answers
        m_opens = no_parsing(cal.session_opens_in_range)
        m_closes = no_parsing(cal.session_closes_in_range)

        # test where start and end are sessions
        start, end = ans.sessions[10], ans.sessions[-10]
        tm.assert_series_equal(m_opens(start, end), ans.opens[10:-9], check_freq=False)
        tm.assert_series_equal(
            m_closes(start, end), ans.closes[10:-9], check_freq=False
        )

        # test session blocks
        for _, block in ans.session_block_generator():
            tm.assert_series_equal(
                m_opens(block[0], block[-1]), ans.opens[block], check_freq=False
            )
            tm.assert_series_equal(
                m_closes(block[0], block[-1]), ans.closes[block], check_freq=False
            )

        # tests where start and end are non-session dates
        if len(ans.non_sessions) > 1:
            # test that range within which there are no sessions returns empty
            start, end = ans.non_sessions_range
            assert m_opens(start, end).empty
            assert m_closes(start, end).empty

            # test range defined with start and end as non-sessions
            (start, end), sessions = ans.sessions_range_defined_by_non_sessions
            tm.assert_series_equal(
                m_opens(start, end), ans.opens[sessions], check_freq=False
            )
            tm.assert_series_equal(
                m_closes(start, end), ans.closes[sessions], check_freq=False
            )
