#!/usr/bin/env python

__author__ = 'Jeremy B. Merrill'
__email__ = 'jeremybmerrill@gmail.com'
__license__ = 'Apache'
__version__ = '0.1'

from onpi import is_on_pi
import logging
LOG_FILENAME = '/tmp/buses.log'
if is_on_pi():
  try:
    logging.basicConfig(filename=LOG_FILENAME,level=logging.DEBUG)
  except IOError:
    logging.basicConfig(level=logging.DEBUG) #stdout
else:
  logging.basicConfig(level=logging.DEBUG) #stdout

from operator import itemgetter
import time
from busstop import BusStop, read_bustime_data_from_disk
import yaml
import os
from ticker import Ticker
import traceback

from terminal_colors import green_code, red_code, yellow_code, blue_code, end_color

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from busstop import Base, TestCompleteException

print("debug?", read_bustime_data_from_disk)

class BigAppleSerialBus:
  is_on_pi = False
  lights = {}

  #The MTA's bustime website pings every 15 seconds, so I feel comfortable doing the same.
  between_checks = 15 if not read_bustime_data_from_disk else 0 #seconds
  between_status_updates = 3 if not read_bustime_data_from_disk else 0  #seconds

  def __init__(self):
    self.is_on_pi = is_on_pi()
    self.__init_db__()
    self.bus_stops = []
    if read_bustime_data_from_disk:
      self.session_errors = [] #only for testing :)

    if self.is_on_pi:
      import RPi.GPIO as GPIO
      GPIO.setmode(GPIO.BCM)
      logging.debug("am running on a Raspberry Pi")

    self.__init_stops__()
    self.__cycle_lights__()
    self.__init_ticker__()

  def __init_stops__(self):
    config_file_path = os.path.join(os.path.dirname(__file__), "../config.yaml")
    config = yaml.load(open(config_file_path, 'r'))
    for info in sorted(config["stops"], cmp=lambda x, y: (-1 * cmp(x["stop"], y["stop"])) if x["route_name"] == y["route_name"] else cmp(x["route_name"], y["route_name"]) ):
      busName = info["route_name"]
      stop_id = info["stop"]
      #find or create stop
      stop = self.session.query(BusStop).filter(BusStop.stop_id == stop_id).filter(BusStop.route_name == busName).first()
      if not stop:
        stop = BusStop(busName, stop_id) #TODO: needs kwargs?
        self.session.add(stop)
      stop.add_attributes(int(info["distance"]), self.session)

      self.bus_stops.append(stop)
      if self.is_on_pi:
        from light import Light
        #create the lights
        self.lights[stop] = {}
        self.lights[stop]['red'] = Light(info["redPin"])
        self.lights[stop]['green'] = Light(info["greenPin"])

  def check_buses(self):
    if not self.bus_stops:
      print(self.session_errors)
      print("buses: " + ','.join(map(lambda a: a[0] + ": " + str(len(a[1])), self.session_errors)))
      print("sums: "  + ','.join(map(lambda a: str(sqrt(sum( map(lambda x: x**2, a[1])) )), self.session_errors)))
      print("rmses: " + ','.join(map(lambda a: str(sqrt(sum( map(lambda x: x**2, a[1])) )/len(a[1])), self.session_errors)))

      raise TestCompleteException("Test complete!")
    for stop in self.bus_stops:
      logging.debug("checking %(route_name)s/%(end_stop_id)s (%(count)i buses on route)" % 
        {'route_name': stop.route_name, 'count': len(stop.buses_on_route), 'end_stop_id': stop.stop_id })
      try:
        trajectories = stop.check()
      except TestCompleteException: #for testing only
        self.session_errors.append((stop.route_name, stop.session_errors))
        self.bus_stops.remove(stop)
        continue
      for traj in [traj for traj in trajectories if traj]:
        logging.debug("writing trajectory:" + str(traj))
        if not read_bustime_data_from_disk: 
          self.session.add(traj)

      self.convert_to_lights(stop)
    self.session.commit()

  def broadcast_status(self):
    if self.is_on_pi:
      self.update_lights()
    # print(' '.join([stop.status() for stop in self.bus_stops]))

  def convert_to_lights(self, bus_stop):
    if not self.is_on_pi:
      return
    if bus_stop.status_error:
      [light.toggle() for light in self.lights[bus_stop]]
    else:
      if bus_stop.bus_is_near:
        self.lights[bus_stop]['green'].on()
      else:
        self.lights[bus_stop]['green'].off()
      if bus_stop.bus_is_imminent:
        self.lights[bus_stop]['red'].on()
      else:
        self.lights[bus_stop]['red'].off()

  def update_lights(self):
    if not self.is_on_pi:
      return
    for bus_stop in self.bus_stops:
      self.convert_to_lights(bus_stop)

  def __init_db__(self):
    """do database crap"""
    sqlite_db_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "../buses.db")
    engine = create_engine('sqlite:///' + sqlite_db_path) #only creates the file if it doesn't exist already
    Base.metadata.create_all(engine)
    Base.metadata.bind = engine
     
    DBSession = sessionmaker(bind=engine)
    self.session = DBSession()

  def __cycle_lights__(self):
    flat_lights = [item for sublist in [d.values() for d in self.lights.values()] for item in sublist]
    for light in flat_lights:
      light.on()
      if not read_bustime_data_from_disk:
        time.sleep(2)
      light.off()

  def __init_ticker__(self):
    ticker = Ticker(None if not read_bustime_data_from_disk else 0)
    ticker.register(self.check_buses, self.between_checks)
    #TODO: only print new status on non-15-sec ticks if it hasn't changed
    ticker.register(self.broadcast_status, self.between_status_updates)
    ticker.global_error(self.__global_error__)
    ticker.start()

  def __global_error__(self, error):
    try:
      self.session.commit()
    except Exception as e:
      logging.debug("unable to save on global error")
    logging.exception('Error:')
    if self.is_on_pi:
      light_pairs = self.lights.values()
      #turn off all the lights.
      for red_light in [light_pair['red'] for light_pair in light_pairs]:
        red_light.off()

      #then blink red to signal a global error condition
      while True:
        for red_light in [light_pair['red'] for light_pair in light_pairs]:
          red_light.on()
        if not read_bustime_data_from_disk:
          time.sleep(5)
        for red_light in [light_pair['red'] for light_pair in light_pairs]:
          red_light.off()
        if not read_bustime_data_from_disk:
          time.sleep(5)
    else:
      print(error)
      raise error

if __name__ == "__main__":
  BigAppleSerialBus()

#TODO: calculate errors.