"""Optional Open-Meteo forecasts, selected into explicit local-time windows."""
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
import time as monotonic_time
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo


class ForecastError(ValueError):
    """Controlled validation reasons, containing no provider response text."""


class WeatherError(ValueError):
    """Safe diagnostic for the news-only fallback."""

    def __init__(self, stage, reason, error, status, attempts, elapsed):
        super().__init__(f'stage={stage} reason={reason} error={error} '
                         f'http_status={status} attempts={attempts} elapsed_seconds={elapsed:.2f}')


def edition_context(now, timezone_name, boundaries):
    if now.tzinfo is None:
        raise ValueError('Generation time must be timezone-aware')
    local = now.astimezone(ZoneInfo(timezone_name))
    ordered = sorted((time.fromisoformat(value), key) for key, value in boundaries.items())
    edition = ordered[-1][1]
    for boundary, name in ordered:
        if local.time().replace(tzinfo=None) >= boundary:
            edition = name
    return {'edition': edition, 'local_time': local.isoformat(),
            'local_date': local.date().isoformat(), 'timezone': timezone_name}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ForecastError('Missing or invalid forecast value')
    return value


def _conditions(code):
    if code == 0:
        return 'clear'
    if code in (1, 2, 3):
        return 'partly cloudy' if code != 3 else 'overcast'
    if code in (45, 48):
        return 'fog'
    if code in (51, 53, 55, 56, 57):
        return 'drizzle'
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return 'rain'
    if code in (71, 73, 75, 77, 85, 86):
        return 'snow'
    if code in (95, 96, 99):
        return 'thunderstorms'
    raise ForecastError('Unknown forecast weather code')


def summarize_forecast(data, settings, timezone_name, now, edition):
    zone = ZoneInfo(timezone_name)
    local = now.astimezone(zone)
    hourly = data['hourly']
    fields = ('temperature_2m', 'precipitation_probability', 'wind_speed_10m', 'weather_code')
    times = hourly['time']
    if not isinstance(times, list) or not times:
        raise ForecastError('Incomplete hourly forecast')
    for field in fields:
        if not isinstance(hourly.get(field), list):
            raise ForecastError(f'Missing hourly {field} array')

    midnight = datetime.combine(local.date() + timedelta(days=1), time(), zone)
    start = local
    if edition == 'morning':
        windows = [('rest_of_today', start, midnight)]
        daily_date = local.date()
    elif edition == 'evening':
        windows = [('remaining_evening', start, midnight),
                   ('tomorrow', midnight, midnight + timedelta(days=1))]
        daily_date = midnight.date()
    else:
        morning = midnight if local.hour >= 12 else datetime.combine(local.date(), time(), zone)
        overnight_end = morning.replace(hour=8)
        windows = [('overnight', start, overnight_end),
                   ('next_morning', morning.replace(hour=6), morning.replace(hour=12))]
        daily_date = morning.date()

    rows = []
    for index, epoch in enumerate(times):
        epoch = _number(epoch)
        if not any(epoch + 3600 > begin.timestamp() and epoch < end.timestamp()
                   for _, begin, end in windows):
            continue
        row = {'time': datetime.fromtimestamp(epoch, zone)}
        for field in fields:
            try:
                row[field] = _number(hourly[field][index])
            except (ForecastError, IndexError):
                raise ForecastError(f'Missing or invalid {field} in requested window') from None
        if not 0 <= row['precipitation_probability'] <= 100 or row['wind_speed_10m'] < 0:
            raise ForecastError('Invalid weather range in requested window')
        rows.append(row)

    imperial = settings['units'] == 'imperial'
    temperature_unit, wind_unit = ('°F', 'mph') if imperial else ('°C', 'km/h')
    expected_temp = '°F' if imperial else '°C'
    if data['hourly_units']['temperature_2m'] != expected_temp or data['hourly_units']['wind_speed_10m'] != wind_unit:
        raise ForecastError('Unexpected forecast units')
    wind_threshold = settings['notable_wind_kmh'] / 1.609344 if imperial else settings['notable_wind_kmh']
    result = {'provider': 'Open-Meteo', 'kind': 'forecast, not observation',
              'location': settings['location'], 'timezone': timezone_name,
              'temperature_unit': temperature_unit, 'wind_unit': wind_unit, 'windows': []}
    for name, begin, end in windows:
        selected = [row for row in rows if row['time'].timestamp() + 3600 > begin.timestamp()
                    and row['time'].timestamp() < end.timestamp()]
        if not selected or selected[0]['time'].timestamp() > begin.timestamp() or selected[-1]['time'].timestamp() + 3600 < end.timestamp():
            raise ForecastError('Forecast does not cover the requested window')
        if any(b['time'].timestamp() - a['time'].timestamp() != 3600
               for a, b in zip(selected, selected[1:])):
            raise ForecastError('Requested window must contain consecutive hourly values')
        rainy = [row for row in selected if row['precipitation_probability'] >= settings['rain_probability_threshold']]
        # Compact contiguous precipitation windows, retaining their maximum probability.
        rain_windows = []
        for row in rainy:
            hour = datetime.fromtimestamp(max(row['time'].timestamp(), begin.timestamp()), zone)
            end_hour = datetime.fromtimestamp(min(row['time'].timestamp() + 3600, end.timestamp()), zone)
            if rain_windows and rain_windows[-1]['end'] == hour.isoformat():
                rain_windows[-1]['end'] = end_hour.isoformat()
                rain_windows[-1]['probability_percent'] = max(rain_windows[-1]['probability_percent'], row['precipitation_probability'])
            else:
                rain_windows.append({'start': hour.isoformat(), 'end': end_hour.isoformat(),
                                     'probability_percent': row['precipitation_probability']})
        max_wind = max(row['wind_speed_10m'] for row in selected)
        result['windows'].append({
            'name': name, 'start': begin.isoformat(), 'end': end.isoformat(),
            'low': min(row['temperature_2m'] for row in selected),
            'high': max(row['temperature_2m'] for row in selected),
            'conditions': list(dict.fromkeys(_conditions(row['weather_code']) for row in selected)),
            'max_precipitation_probability': max(row['precipitation_probability'] for row in selected),
            'precipitation_windows': rain_windows, 'max_wind': max_wind,
            'notable_wind': max_wind >= wind_threshold,
        })
    daily = data['daily']
    daily_index = next((i for i, epoch in enumerate(daily['time'])
                        if datetime.fromtimestamp(epoch, zone).date() == daily_date), None)
    if daily_index is None or data['daily_units']['temperature_2m_max'] != expected_temp or data['daily_units']['temperature_2m_min'] != expected_temp:
        raise ForecastError('Missing daily forecast or wrong units')
    result['daily'] = {'date': daily_date.isoformat(),
                       'high': _number(daily['temperature_2m_max'][daily_index]),
                       'low': _number(daily['temperature_2m_min'][daily_index])}
    return result


def _retry_delay(headers, attempt):
    value = headers.get('Retry-After') if headers else None
    if value:
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except ValueError:
            pass
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return 0.5 * 2 ** (attempt - 1)


def fetch_weather(settings, timezone_name, now, edition, *, opener=urlopen):
    if not settings['enabled']:
        return None
    imperial = settings['units'] == 'imperial'
    query = urlencode({
        'latitude': settings['latitude'], 'longitude': settings['longitude'],
        'hourly': 'temperature_2m,precipitation_probability,wind_speed_10m,weather_code',
        'daily': 'temperature_2m_max,temperature_2m_min', 'forecast_days': 3,
        'temperature_unit': 'fahrenheit' if imperial else 'celsius',
        'wind_speed_unit': 'mph' if imperial else 'kmh',
        'timezone': timezone_name, 'timeformat': 'unixtime',
    })
    started = monotonic_time.monotonic()
    # Three attempts at most, sharing a budget of twice the per-attempt timeout.
    deadline = started + settings['timeout'] * 2
    for attempt in range(1, 4):
        stage, reason, status = 'request', 'Request failed', None
        retryable, headers = False, None
        try:
            remaining = deadline - monotonic_time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with opener('https://api.open-meteo.com/v1/forecast?' + query,
                        timeout=min(settings['timeout'], remaining)) as response:
                raw = response.read(1024 * 1024 + 1)
            stage = 'response'
            if monotonic_time.monotonic() >= deadline:
                reason = 'Forecast fetch exceeded total time budget'
                raise TimeoutError
            if len(raw) > 1024 * 1024:
                raise ForecastError('Forecast response exceeded size budget')
            stage = 'json'
            data = json.loads(raw)
            stage = 'validation'
            result = summarize_forecast(data, settings, timezone_name, now, edition)
            break
        except HTTPError as exc:
            status = exc.code
            retryable = status == 429 or 500 <= status <= 599
            headers = exc.headers
            exc.close()
            error = type(exc).__name__
        except (URLError, OSError) as exc:
            retryable = True
            error = type(exc).__name__
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ForecastError) else 'Invalid forecast response'
            error = type(exc).__name__
        elapsed = monotonic_time.monotonic() - started
        delay = _retry_delay(headers, attempt)
        if retryable and elapsed + delay >= settings['timeout'] * 2:
            reason = 'Retry wait or request exceeded remaining time budget'
        if not retryable or attempt == 3 or elapsed + delay >= settings['timeout'] * 2:
            raise WeatherError(stage, reason, error, status, attempt, elapsed) from None
        monotonic_time.sleep(delay)
    result['fetched_at'] = now.isoformat()
    result['freshness_note'] = 'Fetch time, not model issuance time; Open-Meteo provides no issuance timestamp here.'
    return result
