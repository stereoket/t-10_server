import ephem
import json
import requests
import threading
from calendar import timegm
from datetime import datetime, timedelta
from math import degrees

API_URLS = { 'iss': "http://api.open-notify.org/iss/?lat={0}&lon={1}&alt={2}&n={3}",
             'weather': {'city_now': "http://api.openweathermap.org/data/2.5/weather?q={0}",
                         'coord_now': "http://api.openweathermap.org/data/2.5/weather?lat={0}&lon={1}",
                         'city_forecast': "http://api.openweathermap.org/data/2.5/forecast?q={0}"
                     }
         }

ACS_URLS = { 'notify': "https://api.cloud.appcelerator.com/v1/push_notification/notify.json?key={0}",
             'login': "https://api.cloud.appcelerator.com/v1/users/login.json?key={0}",
             'subscribe': "https://api.cloud.appcelerator.com/v1/push_notifications/subscribe.json?key={0}"
         }

TIMERS = {}

def in_time_of_day(city, pass_time, time_of_day):
    '''Returns sunset and sunrise times for the given city at date'''
    location = ephem.city(city)
    location.date = pass_time
    sun = ephem.Sun()
    if time_of_day == "day":
        previous_rising = location.previous_rising(sun).datetime()
        next_setting = location.next_setting(sun, start=pass_time.date()).datetime()
        return previous_rising.date() == pass_time.date() and pass_time <= next_setting
    elif time_of_day == "night":
        previous_rising = location.previous_rising(sun).datetime()
        previous_setting = location.previous_setting(sun).datetime()
        next_rising = location.next_rising(sun).datetime()
        return (previous_setting.date() == pass_time.date() and pass_time <= next_rising) or (next_rising.date() == pass_time.date() and pass_time <= next_rising)
    else:
        return True

class WeatherData():
    def __init__(self, city):
        self.city = city

    def __do_get(self, url):
        r = requests.get(url)
        try:
            return json.loads(r.text)
        except ValueError:
            return {} # Something went wrong!

    def current_cloud_cover(self):
        url = API_URLS['weather']['city_now'].format(self.city)
        data = self.__do_get(url)
        return data['clouds']['all'] / 100.0

    def cloud_forecast(self, date):
        url = API_URLS['weather']['city_forecast'].format(self.city)
        data = self.__do_get(url)
        forecast = data['list']
        least_diff = 9999999999999
        closest_forecast = None
        for f in forecast:
            time_diff = (date - datetime.utcfromtimestamp(f['dt'])).total_seconds()
            if abs(time_diff) < least_diff:
                least_diff = time_diff
                closest_forecast = f
        return closest_forecast['clouds']['all'] / 100.0

class T10Helper():
    '''"Server" for handling alerts, checking weather, what not.'''
    def __init__(self, acs):
        self.acs = acs

    def get_cloud_cover(self, city):
        '''Gets cloud cover in % for the given city'''
        url = API_URLS['weather']['city_search'].format(city)
        print url
        r = requests.get(url)
        try:
            result = json.loads(r.text)
        except ValueError:
            return '0'
        return result['data']['current_condition'][0]['cloudcover']


    def get_next_passes(self, lat, lon, altitude, count, force_visible=False):
        '''Returns a list of the next visible passes for the ISS'''

        tle_data = requests.get("http://celestrak.com/NORAD/elements/stations.txt").text # Do not scrape all the time for release!
        iss_tle = [str(l).strip() for l in tle_data.split('\r\n')[:3]]

        iss = ephem.readtle(*iss_tle)

        location = ephem.Observer()
        location.lat = str(lat)
        location.long = str(lon)
        location.elevation = altitude

        # Ignore effects of atmospheric refraction
        location.pressure = 0
        location.horizon = '5:00'

        location.date = datetime.utcnow()
        passes = []
        for p in xrange(count):
            tr, azr, tt, altt, ts, azs = location.next_pass(iss)
            duration = int((ts - tr) * 60 * 60 * 24)
            year, month, day, hour, minute, second = tr.tuple()
	    dt = datetime(year, month, day, hour, minute, int(second))
            location.date = tr
            iss.compute(location)
            if not (force_visible and iss.eclipsed):
                passes.append({"risetime": timegm(dt.timetuple()), "duration": duration})
            location.date = tr + 25 * ephem.minute

        return {"response": passes }

    def get_current_iss_location(self):
        '''Returns the current ISS location'''
        tle_data = requests.get("http://celestrak.com/NORAD/elements/stations.txt").text # Do not scrape all the time for release!
        iss_tle = [str(l).strip() for l in tle_data.split('\r\n')[:3]]

        iss = ephem.readtle(*iss_tle)

        now = datetime.utcnow()
        iss.compute(now)
        lon = degrees(iss.sublong)
        lat = degrees(iss.sublat)

        return {'response': {'latitude': lat, 'longitude': lon}}

    def alert_next_passes(self, acc_cloud_cover, timeofday, device_id, count=10, city=None, coord=(0.0, 0.0)):
        '''Sets up alerts for up to the next 10 passes of the ISS over the given city or lat/lon. Alerts will be sent to the device that registered for them'''
        try:
            # Cancel previous timers.
            for t in TIMERS[city]:
                t.cancel()
        except KeyError:
            pass
        finally:
            TIMERS[city] = []
        location = ephem.city(city)
        url = API_URLS['iss'].format(degrees(location.lat), degrees(location.lon), int(location.elevation), count)
        #print url
        #r = requests.get(url)
        #result = json.loads(r.text)
        result = self.get_next_visible_passes(degrees(location.lat), degrees(location.lon), int(location.elevation), count)
        next_passes = result['response']
        # For every pass, set up a trigger for 10 minutes earlier and send it
        # to the 'space' channel
        real_response = []
        for p in next_passes:
            risetime = datetime.utcfromtimestamp(p['risetime'])
            weather_data = WeatherData(city)
            # Skip if the pass is at the wrong time of day
            if not in_time_of_day(city, risetime, timeofday):
                continue
            riseminus10 = risetime - timedelta(minutes=10)
            delay = (riseminus10 - datetime.utcnow()).total_seconds()
            print "Running in {0} seconds...".format(delay)
            def f():
                weather_data = WeatherData(city)
                cloud_cover = weather_data.current_cloud_cover()
                if cloud_cover <= acc_cloud_cover:
                    print "Cloud cover acceptable"
                    self.acs.push_to_ids_at_channel('space', [device_id], json.dumps({'location': city, 'cloudcover': cloud_cover}))
            t = threading.Timer(delay, f)
            TIMERS[city].append(t)
            t.start()
            cloud_forecast = weather_data.cloud_forecast(datetime.utcfromtimestamp(p['risetime']))
            real_response.append({'location': city, 'time_str': str(risetime), 'time': p['risetime'], 'cloudcover': cloud_forecast, 'trigger_time': str(riseminus10)})
            #print real_response
        print "Length:", len(real_response)
        return real_response

class T10ACSHelper():
    '''Handles connections to Appcelerator Cloud Services and does push notifications'''
    def __init__(self, user, password, key):
        self.key = key
        self.user = user
        self.password = password
        self.__login()
        self.clients = {}

    def __login(self):
        '''Need to login to appcelerator'''
        payload = {'login':self.user, 'password':self.password}
        r = requests.post(ACS_URLS['login'].format(self.key), data=payload)
        self.cookies = r.cookies

    def subscribe_device(self, channel, device_type, device_id):
        try:
            self.clients[channel].append(device_id)
        except KeyError:
            self.clients[channel] = [device_id]
            #print self.clients
        finally:
            url = ACS_URLS['subscribe'].format(self.key)
            payload = {'type':device_type, 'device_id':device_id, 'channel':'channel'}
            r = requests.post(url, data=payload, cookies=self.cookies)

    def push_to_channel(self, channel, message):
        try:
            self.push_to_ids_at_channel(channel, self.clients[channel], message)
        except KeyError:
            return

    def push_to_ids_at_channel(self, channel, ids, message):
        print "Pushing {0} to {1}".format(message, channel)
        string_ids = ",".join(ids)
        payload = {'channel':channel, 'to_ids':string_ids, 'payload':json.dumps({'badge':2, 'sound':'default', 'alert':message})}
        url = ACS_URLS['notify'].format(self.key)
        #print url
        r = requests.post(url, data=payload, cookies=self.cookies)
