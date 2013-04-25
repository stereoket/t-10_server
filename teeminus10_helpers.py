import ephem
import json
import requests
from datetime import datetime

API_URLS = { 'iss': "http://api.open-notify.org/iss/?lat={0}&lon={1}&alt={2}&n={3}",
             'weather': {'city_search': "http://api.openweathermap.org/data/2.5/weather?q={0}",
                         'coord_search': "http://api.openweathermap.org/data/2.5/weather?lat={0}&lon={1}" }
         }

ACS_URLS = { 'notify': "https://api.cloud.appcelerator.com/v1/push_notification/notify.json?key={0}",
             'login': "https://api.cloud.appcelerator.com/v1/users/login.json?key={0}",
             'subscribe': "https://api.cloud.appcelerator.com/v1/push_notifications/subscribe.json?key={0}"
         }

TIMERS = {}

def to_decimal(coord):
    '''Convert DDMMSS.SS to DD.MMSSSS'''
    tokens = str(coord).split(":")
    r = abs(int(tokens[0])) + abs(int(tokens[1])/60.0) + abs(float(tokens[2])/3600.0)
    #print r
    return r

def get_nighttime(city):
    '''Returns sunset and sunrise times for the given city'''
    now = datetime.utcnow()
    location = ephem.city(city)
    sun = ephem.Sun()
    location.date = now
    return (location.next_setting(sun).datetime(), location.next_rising(sun).datetime())

class T10Helper():
    '''"Server" for handling alerts, checking weather, what not.'''
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

    def alert_next_passes(self, city, acc_cloud_cover, timeofday, device_id, count):
        '''Sets up alerts for up to the next 10 passes of the ISS over the given city. Alerts will be sent to the device that registered for them'''
        try:
            # Cancel previous timers.
            for t in TIMERS[city]:
                t.cancel()
        except KeyError:
            pass
        finally:
            TIMERS[city] = []
        location = ephem.city(city)
        url = API_URLS['iss'].format(to_decimal(location.lat), to_decimal(location.lon), int(location.elevation), count)
        #print url
        r = requests.get(url)
        result = json.loads(r.text)
        next_passes = result['response']
        # For every pass, set up a trigger for 10 minutes earlier and send it
        # to the 'space' channel
        real_response = []
        for p in next_passes:
            risetime = datetime.utcfromtimestamp(p['risetime'])
            nighttime = get_nighttime(city)
            print timeofday, risetime, nighttime[0], nighttime[1]
            # Skip if the pass is at the wrong time of day
            if timeofday == 'night' and (risetime < nighttime[0] or risetime > nighttime[1]):
                continue
            elif timeofday == 'day' and (risetime >= nighttime[0] or risetime <= nighttime[1]):
                continue
            riseminus10 = risetime - timedelta(minutes=10)
            delay = (riseminus10 - datetime.utcnow()).total_seconds()
            print "Running in {0} seconds...".format(delay)
            def f():
                cloud_cover = self.get_cloud_cover(city)
                if float(cloud_cover) <= float(acc_cloud_cover):
                    print "Cloud cover acceptable"
                    SERVER.push_to_ids_at_channel('space', [device_id], json.dumps({'location': city, 'cloudcover': cloud_cover}))
            t = threading.Timer(delay, f)
            TIMERS[city].append(t)
            t.start()
            cloud_forecast = self.get_cloud_cover(city, str(datetime.utcfromtimestamp(p['risetime']).date()))
            real_response.append({'location': city, 'time_str': str(risetime), 'time': p['risetime'], 'cloudcover': float(cloud_forecast), 'trigger_time': str(riseminus10)})

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