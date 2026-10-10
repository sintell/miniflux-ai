"""Optional Open-Meteo forecasts, selected into explicit local-time windows."""
from datetime import datetime, time, timedelta, timezone
import json
import math
import time as monotonic_time
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo


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
        raise ValueError('Missing or invalid forecast value')
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
    raise ValueError('Unknown forecast weather code')


def summarize_forecast(data, settings, timezone_name, now, edition):
    zone = ZoneInfo(timezone_name)
    local = now.astimezone(zone)
    hourly = data['hourly']
    fields = ('temperature_2m', 'precipitation_probability', 'wind_speed_10m', 'weather_code')
    times = hourly['time']
    if not times or any(len(hourly[field]) != len(times) for field in fields):
        raise ValueError('Incomplete hourly forecast')
    rows = []
    for index, epoch in enumerate(times):
        row = {field: _number(hourly[field][index]) for field in fields}
        row['time'] = datetime.fromtimestamp(_number(epoch), zone)
        if not 0 <= row['precipitation_probability'] <= 100 or row['wind_speed_10m'] < 0:
            raise ValueError('Invalid weather range')
        rows.append(row)
    if any(b['time'].timestamp() - a['time'].timestamp() != 3600 for a, b in zip(rows, rows[1:])):
        raise ValueError('Forecast must contain consecutive hourly values')

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

    imperial = settings['units'] == 'imperial'
    temperature_unit, wind_unit = ('°F', 'mph') if imperial else ('°C', 'km/h')
    expected_temp = '°F' if imperial else '°C'
    if data['hourly_units']['temperature_2m'] != expected_temp or data['hourly_units']['wind_speed_10m'] != wind_unit:
        raise ValueError('Unexpected forecast units')
    wind_threshold = settings['notable_wind_kmh'] / 1.609344 if imperial else settings['notable_wind_kmh']
    result = {'provider': 'Open-Meteo', 'kind': 'forecast, not observation',
              'location': settings['location'], 'timezone': timezone_name,
              'temperature_unit': temperature_unit, 'wind_unit': wind_unit, 'windows': []}
    for name, begin, end in windows:
        selected = [row for row in rows if row['time'].timestamp() + 3600 > begin.timestamp()
                    and row['time'].timestamp() < end.timestamp()]
        if not selected or selected[0]['time'].timestamp() > begin.timestamp() or selected[-1]['time'].timestamp() + 3600 < end.timestamp():
            raise ValueError('Forecast does not cover the requested window')
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
        raise ValueError('Missing daily forecast or wrong units')
    result['daily'] = {'date': daily_date.isoformat(),
                       'high': _number(daily['temperature_2m_max'][daily_index]),
                       'low': _number(daily['temperature_2m_min'][daily_index])}
    return result


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
    # No cache: failure or incomplete/stale coverage simply omits weather upstream.
    with opener('https://api.open-meteo.com/v1/forecast?' + query, timeout=settings['timeout']) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or monotonic_time.monotonic() - started > settings['timeout']:
        raise ValueError('Forecast response exceeded size/time budget')
    result = summarize_forecast(json.loads(raw), settings, timezone_name, now, edition)
    result['fetched_at'] = now.isoformat()
    result['freshness_note'] = 'Fetch time, not model issuance time; Open-Meteo provides no issuance timestamp here.'
    return result
