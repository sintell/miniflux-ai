from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock
import unittest
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from services.weather import edition_context, fetch_weather, summarize_forecast


SETTINGS = {'enabled': True, 'location': 'Test city', 'latitude': 52, 'longitude': 13,
            'units': 'metric', 'timeout': 5, 'max_age_minutes': 30,
            'rain_probability_threshold': 30, 'notable_wind_kmh': 30}
BOUNDARIES = {'morning': '05:00', 'evening': '12:00', 'night': '21:00'}


def fixture(now, zone='UTC', imperial=False):
    local = now.astimezone(ZoneInfo(zone)).replace(hour=0, minute=0, second=0, microsecond=0)
    times = [local.timestamp() + hour * 3600 for hour in range(74)]
    temp = '°F' if imperial else '°C'
    return {'hourly': {'time': times, 'temperature_2m': [10 + h % 12 for h in range(74)],
                       'precipitation_probability': [70 if h % 24 in (9, 10) else 10 for h in range(74)],
                       'wind_speed_10m': [35] * 74, 'weather_code': [3] * 74},
            'hourly_units': {'temperature_2m': temp, 'wind_speed_10m': 'mph' if imperial else 'km/h'},
            'daily': {'time': [(local + timedelta(days=i)).timestamp() for i in range(3)],
                      'temperature_2m_max': [23, 24, 25], 'temperature_2m_min': [7, 8, 9]},
            'daily_units': {'temperature_2m_max': temp, 'temperature_2m_min': temp}}


class WeatherTests(unittest.TestCase):
    def test_editions_and_dates_use_configured_timezone(self):
        for hour, expected in ((4, 'night'), (5, 'morning'), (12, 'evening'), (21, 'night')):
            now = datetime(2026, 10, 10, hour, tzinfo=timezone.utc)
            self.assertEqual(edition_context(now, 'UTC', BOUNDARIES)['edition'], expected)
        result = edition_context(datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc), 'Asia/Tokyo', BOUNDARIES)
        self.assertEqual(result['local_date'], '2026-10-11')
        self.assertEqual(result['edition'], 'morning')

    def test_morning_has_daily_extrema_and_rain_timing(self):
        now = datetime(2026, 10, 10, 7, 30, tzinfo=timezone.utc)
        result = summarize_forecast(fixture(now), SETTINGS, 'UTC', now, 'morning')
        self.assertEqual(result['daily'], {'date': '2026-10-10', 'high': 23, 'low': 7})
        rain = result['windows'][0]['precipitation_windows']
        self.assertEqual(len(rain), 1)
        self.assertIn('09:00', rain[0]['start'])
        self.assertIn('11:00', rain[0]['end'])
        self.assertTrue(result['windows'][0]['notable_wind'])

    def test_current_hour_precipitation_is_clipped_to_remaining_window(self):
        now = datetime(2026, 10, 10, 9, 30, tzinfo=timezone.utc)
        result = summarize_forecast(fixture(now), SETTINGS, 'UTC', now, 'morning')
        window = result['windows'][0]
        self.assertEqual(window['start'], now.isoformat())
        self.assertEqual(window['precipitation_windows'][0]['start'], now.isoformat())

    def test_evening_and_night_cross_midnight_correctly(self):
        for hour, edition, expected_date in ((18, 'evening', '2026-10-11'), (22, 'night', '2026-10-11'),
                                              (1, 'night', '2026-10-10')):
            now = datetime(2026, 10, 10, hour, tzinfo=timezone.utc)
            result = summarize_forecast(fixture(now), SETTINGS, 'UTC', now, edition)
            self.assertEqual(result['daily']['date'], expected_date)
            self.assertEqual(len(result['windows']), 2)
            self.assertTrue(result['windows'][1]['start'].startswith(expected_date))

    def test_dst_forecast_uses_absolute_hour_sequence(self):
        now = datetime(2026, 10, 24, 22, tzinfo=ZoneInfo('Europe/Berlin'))
        result = summarize_forecast(fixture(now, 'Europe/Berlin'), SETTINGS, 'Europe/Berlin', now, 'night')
        self.assertTrue(result['windows'][0]['start'].endswith('+02:00'))
        self.assertTrue(result['windows'][0]['end'].endswith('+01:00'))

    def test_stale_missing_and_wrong_units_are_rejected(self):
        now = datetime(2026, 10, 10, 7, 30, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            summarize_forecast(fixture(now - timedelta(days=5)), SETTINGS, 'UTC', now, 'morning')
        data = fixture(now)
        data['hourly']['temperature_2m'][10] = None
        with self.assertRaises(ValueError):
            summarize_forecast(data, SETTINGS, 'UTC', now, 'morning')
        with self.assertRaises(ValueError):
            summarize_forecast(fixture(now, imperial=True), SETTINGS, 'UTC', now, 'morning')

    def test_imperial_request_has_coordinates_timeout_and_forecast_provenance(self):
        now = datetime(2026, 10, 10, 7, 30, tzinfo=timezone.utc)
        response = Mock()
        response.read.return_value = json.dumps(fixture(now, imperial=True)).encode()
        opener = Mock()
        opener.return_value.__enter__ = Mock(return_value=response)
        opener.return_value.__exit__ = Mock(return_value=False)
        result = fetch_weather(SETTINGS | {'units': 'imperial'}, 'UTC', now, 'morning', opener=opener)
        query = parse_qs(urlsplit(opener.call_args.args[0]).query)
        self.assertEqual(query['temperature_unit'], ['fahrenheit'])
        self.assertEqual(query['wind_speed_unit'], ['mph'])
        self.assertEqual(opener.call_args.kwargs['timeout'], 5)
        self.assertEqual(result['fetched_at'], now.isoformat())
        self.assertIn('not observation', result['kind'])
        self.assertEqual(result['temperature_unit'], '°F')

    def test_disabled_weather_never_uses_network(self):
        opener = Mock()
        self.assertIsNone(fetch_weather(SETTINGS | {'enabled': False}, 'UTC', datetime.now(timezone.utc), 'morning', opener=opener))
        opener.assert_not_called()
