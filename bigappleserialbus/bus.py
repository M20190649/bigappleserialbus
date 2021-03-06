#!/usr/bin/env python

__author__ = 'Jeremy B. Merrill'
__email__ = 'jeremybmerrill@gmail.com'
__license__ = 'Apache'
__version__ = '0.1'

from datetime import datetime, timedelta
# import time
from trajectory import Trajectory
from itertools import tee, izip
from collections import OrderedDict

from sqlalchemy import Column, ForeignKey, Integer, String, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import orm

# from pylab import plot,show
from numpy import vstack,array
from numpy.random import rand
from scipy.cluster.vq import kmeans,vq
import kmodes
from sklearn.neighbors import NearestNeighbors

import logging #magically the same as the one in bigappleserialbus.py

default_bus_speed = 4 # m/s ~= 8 miles per hour
#sometimes the bus, at the terminal where it starts, reports itself as, e.g. 0.2 meters along the route.
#this is used to decide that, yes, it's still at the start of the route.
max_gps_error = 20 #meters

# any segment longer than this disqualifies the trajectory, since something went wonky here
MAX_SEGMENT_TIME = 300 
MIN_SEGMENT_TIME = 20

class Bus:
  def __init__(self, number, journey, route_name, end_stop_id, session):
    self.number = number
    self.time_location_pairs = []

    self.stop_time_pairs = OrderedDict() #store time along the route
    self.start_time = None
    self.stops = []
    self.stop_distances = {}
    self.previous_bus_positions = []
    self.db_session = session
    self.route_name = route_name
    self.end_stop_id = end_stop_id
    self.red_light_time = None
    self.green_light_time = None
    self.seconds_away = None
    self.error = None

    self.first_projected_arrival = datetime.min
    self.first_projected_arrival_speeds = 0
    self.set_trajectory_points(journey)

  def __repr__(self):
    seconds_away_str = ''
    if self.seconds_away :
      seconds_away_str = " %(sec)i s/a" %  { 'sec': self.seconds_away }
    if self.first_projected_arrival and self.seconds_away:
      seconds_away_str += ", FP: %(fp)s" % {'fp': str(self.first_projected_arrival)[11:19]}

    return "<Bus #%(number)s%(full_data)s %(route)s/%(stop)s%(sec)s>" % {
          'number': self.number,
          'full_data': '' if self.has_full_data else '*',
          'route': self.route_name,
          'stop': self.end_stop_id,
          'sec': seconds_away_str
        }

  def add_observed_position(self, journey, recorded_at_str):
    """tk"""
    bus_position = {
      'recorded_at': datetime.strptime(recorded_at_str[:19], "%Y-%m-%dT%H:%M:%S"), #recorded_at
      'next_stop': journey["OnwardCalls"]["OnwardCall"][0]["StopPointRef"], #next_stop_ref
      'next_stop_name': journey["OnwardCalls"]["OnwardCall"][0]["StopPointName"],
      'distance_along_route': journey["MonitoredCall"]["Extensions"]["Distances"]["CallDistanceAlongRoute"] - journey["MonitoredCall"]["Extensions"]["Distances"]["DistanceFromCall"],
      'distance_to_end': journey["MonitoredCall"]["Extensions"]["Distances"]["DistanceFromCall"], #distance_from_call
      'distance_to_next_stop': journey["OnwardCalls"]["OnwardCall"][0]["Extensions"]["Distances"]["DistanceFromCall"],
      'is_at_stop': journey["OnwardCalls"]["OnwardCall"][0]["Extensions"]["Distances"]["PresentableDistance"] == "at stop",
    }
    self._add_observed_position(bus_position)

  def _add_observed_position(self, bus_position):
    """From a bus_position, object, update the bus's internal representation of its location and previous trajectory."""
    bus_position['is_underway'] = bus_position['next_stop'] in self.stops and self.stops.index(bus_position['next_stop']) >= 0
    #legacy crap, for speed stuff
    if not (self.time_location_pairs and self.time_location_pairs[0][0] == bus_position['recorded_at']):
      self.time_location_pairs.insert(0, [bus_position['recorded_at'], bus_position['distance_to_end']])
    if not bus_position['is_underway']:
      return;

    if not self.previous_bus_positions:
      self.previous_bus_positions.append(bus_position)
      return

    previous_bus_position = self.previous_bus_positions[-1]
    self.previous_bus_positions.append(bus_position)

    # if this bus_position hasn't been updated since the last check, skip it.
    if previous_bus_position['recorded_at'] == bus_position['recorded_at']:
      return
    # if the bus hasn't moved (i.e. the current next stop has already been visited)
    if self.stop_time_pairs and self.stop_time_pairs[bus_position['next_stop']]:
      return
    # as soon as the bus starts moving away from its start point. 
    # (Don't count as its start_time time it spends going the opposite direction)
    if not self.start_time and bus_position['is_underway']:
      self.start_time = bus_position['recorded_at']

    #if we've passed the next stop (i.e. the first key with None as its value), interpolate its value

    #TODO: test this real good.
    for missed_stop in self.stops[:self.stops.index(bus_position['next_stop'])]:
      if self.stop_time_pairs[missed_stop] is None:
        distance_traveled = previous_bus_position['distance_to_end'] - bus_position['distance_to_end']
        time_elapsed = bus_position['recorded_at'] - previous_bus_position['recorded_at']
        assert time_elapsed.seconds > 0
        print("%(bus_name)s add_observed_position interpolated; next stop: %(stop_ref)s, so prev_stop: %(missed)s @ %(missed_dist)s away" % 
          {'bus_name': self.number, 'stop_ref': bus_position['next_stop'], 'missed': missed_stop, 'missed_dist': self.stop_distances[self.stops[-1]] - self.stop_distances[missed_stop]})
        # print("distance: prev: %(prev_loc)fm, this: %(this_loc)fm; prev_dist: %(prev_dist)f; curtime: %(currec)s, prev: %(prevrec)s" % 
        #   {'prev_loc': previous_bus_position['distance_to_end'], 'this_loc': bus_position['distance_to_end'], 
        #   'prev_dist': previous_bus_position['distance_to_next_stop'], 'prevrec':previous_bus_position['recorded_at'], 'currec': bus_position['recorded_at']})
                
        # explanation of what's going on here
        #
        # bust_pos-----S------S-----bus_pos
        # 0sec                      100 sec
        # 0m          150m   320m   600m
        # assume a constant speed
        # 100 sec here is time_elapsed.seconds
        # 600m is distance_traveled
        # 150m is (for first stop) self.stop_distances[missed_stop] - previous_bus_position['distance_along_route']
        distance_to_missed_stop = int(self.stop_distances[missed_stop] - previous_bus_position['distance_along_route'])
        if distance_to_missed_stop < 0:
          print(self.number, missed_stop, bus_position['next_stop'], self.stop_distances[missed_stop], previous_bus_position['distance_along_route'])
        assert(distance_to_missed_stop >= 0)

        time_to_missed_stop = int(time_elapsed.seconds * (float(distance_to_missed_stop) / distance_traveled) )
        if not time_to_missed_stop >= 0:
          logging.debug("time_to_missed_stop < 0: " + str(time_to_missed_stop) + " (" + str(time_elapsed.seconds) + " * " + str(distance_to_missed_stop) + " / " + str(distance_traveled) + ")")
        assert(time_to_missed_stop >= 0)
        print("prev/curr dist: %(prev_dist)f/%(curr_dist)f, time elapsed: %(time_elapsed)i, time to stop: %(time_to)i" %
          {'prev_dist': previous_bus_position['distance_to_end'], 'curr_dist': bus_position['distance_to_end'], 
           'time_elapsed': time_elapsed.seconds, 'time_to': time_to_missed_stop})

        interpolated_prev_stop_arrival_time = timedelta(seconds=time_to_missed_stop) + previous_bus_position['recorded_at']
        self.stop_time_pairs[missed_stop] = interpolated_prev_stop_arrival_time
    
    #if we're at a stop, add it to the stop_time_pairs 
    # (being at_stop and needing to interpolate the previous stop are not mutually exclusive.)
    if self.stops.index(bus_position['next_stop']) > 0 and bus_position['is_at_stop']:
      self.stop_time_pairs[bus_position['next_stop']] = bus_position['recorded_at']
      # print("%(bus_name)s add_observed_position at stop" % {'bus_name': self.number})

    # Buses often lay over at the first stop, so we record the *last* time it as at the stop.
    first_stop = self.stops[0]
    if self.stops.index(bus_position['next_stop']) == 1 and self.stop_time_pairs[first_stop] is None:
      self.stop_time_pairs[first_stop] = previous_bus_position['recorded_at']
      # print("%(bus_name)s add_observed_position at stop 1" % {'bus_name': self.number})

    print(self.number + str(self.stop_time_pairs))
    print(self.number + " stop_time_pairs at " + str(bus_position['next_stop']) + " set to " + str(self.stop_time_pairs[bus_position['next_stop']]))
    # print the progress so far.
    # print(self.number + ": ")
    # print([(stop_ref, self.stop_time_pairs[stop_ref].strftime("%H:%M:%S")) if self.stop_time_pairs[stop_ref] else (stop_ref,) for stop_ref in self.stops ])
    # print('')

  def fill_in_last_stop(self, recorded_at_str):
    """Fill in the last element in the stop_time_pairs.

       If the bus doesn't stop at the last stop (i.e. the one the user selected as their "home" stop),
       (or if the bus stopped, but not when we checked for it), the bus will be ignored and add_observed_position
       won't be called, and then the final member of stop_time_pairs won't get set. Then we won't be able to 
       save the bus as a trajectory. This method fixes the last element in this circumstance.

       We don't have a "journey" in that case. 
    """
    # if a bus stops appearing the API responses, but never got any values filled in
    # (e.g. because it ran a route in the other direction than what we're following, then left service)
    # don't try to "interpolate" its entire trajectory
    if(False not in [i == None for i in self.stop_time_pairs.values()]):
      print(self.number + " didn't fill in last stop")
      return

    print(self.number + " filling in last stop")
    bus_position = {
      'recorded_at': datetime.strptime(recorded_at_str[:19], "%Y-%m-%dT%H:%M:%S"), #recorded_at
      'next_stop': self.stops[-1],
      'distance_to_end': 0.0,
      'distance_along_route': self.stop_distances[self.stops[-1]],
      'distance_to_next_stop': 0.0,
      'is_at_stop': True,
    }
    self._add_observed_position(bus_position)

    # # if the only None in stop_time_pairs is at the end (for the last stop)
    # ordered_times = [self.stop_time_pairs[stop] for stop in self.stops]
    # if None in ordered_times and ordered_times.index(None) == (len(self.stop_time_pairs)-1):
    #   #if we've passed the final stop stop, fill in its value with now
    #   self.stop_time_pairs[self.stops[-1]] = recorded_at


  # this just fills in the keys to self.stop_time_pairs and members of self.stops
  # called only on init.
  def set_trajectory_points(self, journey):
    starting_distance_along_route = journey["OnwardCalls"]["OnwardCall"][0]["Extensions"]["Distances"]["CallDistanceAlongRoute"]
    if starting_distance_along_route < max_gps_error:
      # print("%(bus_name)s at start: (%(dist)f m away)" % {'bus_name': self.number, 'dist': starting_distance_along_route} )
      self.has_full_data = True
    else:
      print("%(bus_name)s added mid-route: (%(dist)f m along route)" % {'bus_name': self.number, 'dist': starting_distance_along_route} )
      self.has_full_data = False

    for index, onward_call in enumerate(journey["OnwardCalls"]["OnwardCall"]):
      stop_ref = onward_call["StopPointRef"]
      distance_along_route = onward_call["Extensions"]["Distances"]["CallDistanceAlongRoute"]
      if stop_ref not in self.stops:
        # i = stop_ref #IntermediateStop(self.route_name, stop_ref, onward_call["StopPointName"])
        self.stops.append(stop_ref)
        self.stop_distances[stop_ref] = distance_along_route
        self.stop_time_pairs[stop_ref] = None
        assert index == 0 or distance_along_route >= self.stop_distances[self.stops[index-1]] #distances should increase, ensuring the stops are in order
      if index == 0:
        self.stop_time_pairs[stop_ref] = self.start_time
      if stop_ref == journey["MonitoredCall"]["StopPointRef"]:
        break

  # called when we're done with the bus (i.e. it's passed the stop we're interested in)
  def convert_to_trajectory(self, route_name, stop_id):
    # print("%(bus_name)s converting to trajectory" % {'bus_name': self.number})

    segment_intervals = self.segment_intervals()
    if None in segment_intervals: # not ready to be converted to trajectory; because a stop doesn't have time data.
      print("%(bus_name)s trajectory conversion failed 1: %(segs)s " %{'bus_name': self.number, 'segs': segment_intervals})
      return None
    if not self.has_full_data:
      print("%(bus_name)s trajectory conversion failed 2" % {'bus_name': self.number})
      return None
    # print("%(bus_name)s converted to trajectory with segment_intervals: " % {'bus_name': self.number})
    # print(segment_intervals)

    traj = Trajectory(route_name, stop_id, self.start_time)
    traj.set_segment_intervals(segment_intervals)
    traj.green_light_time = self.green_light_time
    traj.red_light_time = self.red_light_time
    traj.error = self.error
    return traj

  def segment_intervals(self):
    if not self.stop_time_pairs:
      return None
    times = []
    segment_intervals = []
    for stop in self.stops:
      times.append(self.stop_time_pairs[stop])
    for time1, time2 in pairwise(times):
      if time1 is not None and time2 is not None:
        segment_intervals.append((time2 - time1).seconds)
      else:
        segment_intervals.append(None)
    return segment_intervals  

  def find_similar_trajectories(self):
    trajs = self.db_session.query(Trajectory.start_time, Trajectory.segment0,Trajectory.segment1,Trajectory.segment2,Trajectory.segment3,Trajectory.segment4,
      Trajectory.segment5,Trajectory.segment6,Trajectory.segment7,Trajectory.segment8,Trajectory.segment9,Trajectory.segment10,
      Trajectory.segment11,Trajectory.segment12,Trajectory.segment13,Trajectory.segment14,Trajectory.segment15,
      Trajectory.segment16,Trajectory.segment17,Trajectory.segment18,Trajectory.segment19,Trajectory.segment20,
      Trajectory.segment21,Trajectory.segment22,Trajectory.segment23,Trajectory.segment24,Trajectory.segment25,
      Trajectory.segment26,Trajectory.segment27,Trajectory.segment28,Trajectory.segment29,Trajectory.segment30,
      Trajectory.segment31,Trajectory.segment32,Trajectory.segment33,Trajectory.segment34,Trajectory.segment35,
      Trajectory.segment36,Trajectory.segment37,Trajectory.segment38,Trajectory.segment39).filter(Trajectory.route_name==self.route_name).filter(Trajectory.end_stop_id == self.end_stop_id)
    
    trajs = [traj for traj in trajs if not any(map(lambda x: x != None and (x > MAX_SEGMENT_TIME or x < MIN_SEGMENT_TIME), traj[1:])) ]
    # TODO: before filtering based on similarity by segments, filter by time.

    similar_trajectories_by_time = self.filter_by_time(trajs)
    similar_trajectories_by_time = [traj[1:] for traj in similar_trajectories_by_time] #remove the time item.
    if not similar_trajectories_by_time:
      return {'similar': [], 'seconds_away': -1}


    # this "backup" method was in use until 1/15/15  (Next 19 lines)
    # backoff: if there's tons of trajectories, make a maximum of N clusters (max_clusters)
    # if N clusters would make "my" cluster contain M trajectories and M < minimum_similar_trajectories
    #   then try again with N_2 as N_1 / 2
    # 
    # How did I compute max_clusters? 
    # count of time_periods * count of weather variables * weekday_types * 4
    # time_periods = early-morning-rush, late-morning-rush, late-morning, early-afternoon, mid-afternoon, early-evening-rush, late-evening-rush, late-evening, overnight
    # weather_variables: hot, cold, rainy, snowy
    # weekday_types: weekday, weekend
    # max_clusters = 288
    # minimum_similar_trajectories = 5
    # similar_trajectories = self.filter_by_segment_intervals(similar_trajectories_by_time, max_clusters)
    # clusters_cnt = max_clusters
    # while clusters_cnt > 1 and len(similar_trajectories) < minimum_similar_trajectories and len(similar_trajectories) > 0:
    #   # logging.debug(' '.join(map(str, ["backing off, with cluster count", clusters_cnt, "too few similar trajectories", len(similar_trajectories), "from",len(similar_trajectories_by_time), "total"])))
    #   clusters_cnt = clusters_cnt / 2
    #   similar_trajectories = self.filter_by_segment_intervals(similar_trajectories_by_time, clusters_cnt)

    similar_trajectories = self.filter_by_segment_intervals(similar_trajectories_by_time, 144)

    if not similar_trajectories:
      return {'similar': [], 'seconds_away': -1}


    segment_intervals = self.segment_intervals()
    last_defined_segment_index = segment_intervals.index(None) if None in segment_intervals else len(segment_intervals)
    # average time-to-home-stop of the similar trajectories
    remaining_times_on_similar_trajectories = [sum(traj[last_defined_segment_index:]) for traj in similar_trajectories]

    # two methods of determining the remaining time from the similar trajectories
    # average the remaining times
    seconds_away = sum(remaining_times_on_similar_trajectories) / len(similar_trajectories)
    # sum the medians for each remaining segment
    # seconds_away =  sum([median(list(x)) for x in zip(*[traj[last_defined_segment_index:] for traj in similar_trajectories])])


    # similar_trajectories_time_elapsed = [sum(traj[:last_defined_segment_index]) for traj in similar_trajectories]  #for sanity checking only
    # logging.debug('remaining times ['+str(last_defined_segment_index)+' / '+str(seconds_away)+'] ('+self.previous_bus_positions[-1]["next_stop_name"]+'): ' + ', '.join([str(i) for i in remaining_times_on_similar_trajectories]))
    # similar_trajectories_time_elapsed = [sum(traj[:last_defined_segment_index]) for traj in similar_trajectories]  #for sanity checking only
    # logging.debug('elapsed times ['+str(last_defined_segment_index)+' / '+str(seconds_away)+'] ('+self.previous_bus_positions[-1]["next_stop_name"]+'): ' + ', '.join([str(i) for i in similar_trajectories_time_elapsed]))


    self.seconds_away = seconds_away
    return {'similar': similar_trajectories, 'seconds_away': seconds_away}

  def filter_by_time(self, trajs):
    if self.start_time is None:
      return trajs
    def to_time_of_day(time):
      if time.hour in [7,8,9]:
        return 0
      elif time.hour in [17,18,19]:
        return 1
      elif time.hour in [10,11,12,13,14,15,16]:
        return 2
      elif time.hour in  [20,21,22,23,0,1,2,3,4,5,6]:
        return 3
    is_a_weekend = self.start_time.weekday() in [5,6]
    by_day = filter(lambda traj: (traj[0].weekday() in [5,6]) == is_a_weekend , trajs)
    if not is_a_weekend:
      time_of_day = to_time_of_day(self.start_time)
      by_time_of_day = filter(lambda traj: to_time_of_day(traj[0]) == time_of_day, by_day)
    else:
      by_time_of_day = by_day
    return by_time_of_day

  def filter_by_segment_intervals(self, trajs, number_of_clusters):
    truncate_trajs_to = trajs[0].index(None)
    trajs = [traj[:truncate_trajs_to] for traj in trajs]

    segment_intervals = self.segment_intervals()
    if segment_intervals is None or all([seg is None for seg in  segment_intervals]):
      return []
    #truncate to last defined point of this bus (i.e. where it is now) to find similar trajectories _so far_.
    # print('%(bus_name)s segment_intervals: ' % {'bus_name': self.number} + ', '.join(map(str, segment_intervals)))
    last_defined_segment_index = segment_intervals.index(None) if None in segment_intervals else len(segment_intervals)
    truncated_trajectories_list = [traj[:last_defined_segment_index] for traj in trajs]
    truncated_trajectories_list = map(preprocess_trajectory, truncated_trajectories_list)
    truncated_trajectories = array(truncated_trajectories_list)
    truncated_segment_intervals = preprocess_trajectory(segment_intervals[:last_defined_segment_index])
    if False: # knearestneighbors is mostly untested
      similar_trajectory_indexes = find_similar_by_kmeans(truncated_trajectories, truncated_segment_intervals, number_of_clusters)
    else:
      similar_trajectory_indexes = find_similar_by_k_nearest_neighbors(truncated_trajectories, truncated_segment_intervals)
    similar_trajectories = [trajs[i] for i in similar_trajectory_indexes]
    return similar_trajectories




  #called when a bus's lights are turned off, when there's not time to make it to the bus
  def too_late(self):
    pass

  #called when a bus's lights are turned red, when there's just enough time to make it to the bus
  def imminent(self):
    self.red_light_time = self.previous_bus_positions[-1]['recorded_at']

  #called when a bus's lights are turned green, when it's time to get ready to go to the bus
  def near(self):
    self.green_light_time = self.previous_bus_positions[-1]['recorded_at']

#TODO: erase all of this below here (at this indent level)



  def get_meters_away(self):
    return self.time_location_pairs[0][1]

  def get_seconds_away(self):
    speed = self.get_speed_mps()
    if speed == 0.0:
      return 6000 # a big number of seconds
    return self.get_meters_away() / speed

  def get_minutes_away(self):
    return timedelta(seconds=self.get_seconds_away())

  def get_speed_mph(self):
    return (self.get_speed_mps() * (60 * 60)) / 1609.34

  def get_speed_mps(self):
    #meters per second
    # this is a rolling weighted average over the past distance_to_track time/position values
    if len(self.time_location_pairs) < 2:
      return default_bus_speed

    centroid = 3.0
    speed_sum = 0
    weight_sum = 0
    for i, (time, location) in enumerate(self.time_location_pairs):
      if i == 0:
        continue;
      weight = centroid / (abs(i - centroid) if abs(i - centroid) > 0 else 0.5)
      weight_sum += weight
      speed_sum += self.naive_speed(0, i) * weight
    meters_per_second = speed_sum / weight_sum
    return meters_per_second

  # def old_get_speed(self):
  #   if len(self.time_location_pairs) < 2:
  #     return default_bus_speed
  #   long_term = self.naive_speed(0, 9)
  #   medium_term = self.less_naive_speed(0, 4)
  #   mid_to_short_term = self.less_naive_speed(0, 2)
  #   short_term = self.less_naive_speed(0, 1) #ignore this, since it might be stuck at a light
  #   meters_per_second = ( (mid_to_short_term * 2) + (medium_term * 2) + long_term) / 5
  #   return meters_per_second

  def naive_speed(self, start_index, end_index):
    if end_index >= len(self.time_location_pairs):
      end_index = -1

    start = self.time_location_pairs[start_index]
    end = self.time_location_pairs[end_index]
    distance = float(abs(start[1] - end[1]))
    time = abs(start[0] - end[0])
    if time.seconds == 0:
      return 0
    return distance / float(time.seconds)

  def less_naive_speed(self, start_index, end_index):
    #naive speed, except don't count time the bus spends stopped
    if end_index >= len(self.time_location_pairs):
      end_index = -1

    start = self.time_location_pairs[start_index]
    end = self.time_location_pairs[end_index]
    distance = float(abs(start[1] - end[1]))
    raw_time = abs(start[0] - end[0])

    for (a_time, a_dist), (b_time, b_dist) in pairwise(self.time_location_pairs):
      if abs(a_dist - b_dist) < 20:
        raw_time -= abs(a_time - b_time)

    return distance / float(time.seconds)

def preprocess_trajectory(traj):
  """Transform/preprocess a trajectory somehow for use in the kmeans algo"""
  new_traj = list(traj)
  # old = list(traj[0:len(traj)/3])
  # medium = list(traj[len(traj)/3:(2*len(traj))/3])
  # new = list(traj[(2*len(traj))/3:])

  # # # in this, case multiply the trailing intervals to make them have more bearing on the output of the kmeans
  # # new_traj = old + medium * 2 + new * 3

  # new_traj = [(o / 3.0) for o in old] + [(o / 2.0) for o in old] + new
  new_traj = traj[-8:]
  # try scaling down the first few
  return new_traj


def find_similar_by_kmeans(truncated_trajectories, truncated_segment_intervals, number_of_clusters=144):
  print("kmeansing")
  centroids,_ = kmeans(truncated_trajectories, number_of_clusters) 
  print("vqing")
  cluster_indices,_ = vq(truncated_trajectories,centroids)
  print("vqing again")
  my_cluster_indices, _ = vq(array([truncated_segment_intervals]), centroids)
  my_cluster_index = my_cluster_indices[0]
  print("done with ML")
  logging.debug("clusters: [%(sizes)s]" % 
    {"sizes": ', '.join([str(cluster_indices.tolist().count(idx)) + ("*" if idx == my_cluster_index else "") for idx in set(sorted(cluster_indices))])})
  
  similar_trajectory_indexes = [i for i in range(0, len(cluster_indices)) if cluster_indices[i] == my_cluster_index]

  # #find the suspiciously-large cluster... that might be the problem
  # large_cluster_indices = [idx for idx in set(sorted(cluster_indices)) if cluster_indices.tolist().count(idx) > 1000] 
  # for i, traj in enumerate(trajs):
  #   if cluster_indices[i] in large_cluster_indices:
  #     if rand() > 0.995:  #5 in 1000
  #       logging.debug("large cluster member: " + str(traj))
  return similar_trajectory_indexes

def find_similar_by_k_nearest_neighbors(truncated_trajectories, truncated_segment_intervals, k=None):
  if not k:
    #k = int(len(truncated_trajectories)**0.5)
    k = 10
  k = min(k, len(truncated_trajectories))
  nbrs = NearestNeighbors(n_neighbors=k, algorithm='ball_tree').fit(truncated_trajectories)
  distances, indices = nbrs.kneighbors(array(truncated_segment_intervals))
  my_nearest_neighbors_indices = indices[0]
  # indices is, for each point in the argument, a list of the index of its nearest neighbors
  # in, presumably, what was sent to fit.    
  return my_nearest_neighbors_indices


def pairwise(iterable):
  "s -> (s0,s1), (s1,s2), (s2, s3), ..."
  a, b = tee(iterable)
  next(b, None)
  return izip(a, b)

def median(array):
  array_len = len(array)
  if not array_len:
    return 0
  if array_len % 2 == 0:
    idx = array_len / 2
    idx2 = idx-1
    return (array[idx] + array[idx2]) /2
  else:
    idx = array_len / 2
    return array[idx]
