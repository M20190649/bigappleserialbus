import time

class Ticker:

  def __init__(self, betweenTicks=1):
    """Returns a ticker. Optionally set the amount of time per tick."""
    self.tickers = {}
    self.ticksSoFar = 0
    self.error_callbacks = []
    self.betweenTicks = betweenTicks #time in seconds

  def __tick__(self):
    for func, frequency in self.tickers.iteritems():
      if self.ticksSoFar % frequency == 0:
        func()

  def register(self, function, frequency):
    """Set a function to be executed once per `frequency` ticks"""
    self.tickers[function] = frequency

  def start(self):
    try:
      while True:
        start_time = time.time()
        self.__tick__()
        duration = time.time() - start_time
        time.sleep(max(self.betweenTicks - duration, 0))
        self.ticksSoFar += 1
    except Exception as e:
      for error_callback in self.error_callbacks:
        error_callback(e)

  def global_error(self, func):
    self.error_callbacks.append(func)