import math
import functools
import datetime
import sys
import pyactiveresource
import pyactiveresource.formats
import simplejson
import singer
from singer import utils
from tap_shopify.context import Context
import time

LOGGER = singer.get_logger()

RESULTS_PER_PAGE = 250

# We've observed 500 errors returned if this is too large (30 days was too
# large for a customer)
DATE_WINDOW_SIZE = 1

# We will retry a 500 error a maximum of 5 times before giving up
MAX_RETRIES = 5

def is_not_status_code_fn(status_code):
    def gen_fn(exc):
        if getattr(exc, 'code', None) and exc.code not in status_code:
            return True
        # Retry other errors up to the max
        return False
    return gen_fn

def leaky_bucket_handler(details):
    LOGGER.info("Received 429 -- sleeping for %s seconds",
                details['wait'])

def retry_handler(details):
    LOGGER.info("Received 500 or retryable error -- Retry %s/%s",
                details['tries'], MAX_RETRIES)

#pylint: disable=unused-argument
def retry_after_wait_gen(**kwargs):
    # This is called in an except block so we can retrieve the exception
    # and check it.
    exc_info = sys.exc_info()
    resp = exc_info[1].response
    # Retry-After is an undocumented header. But honoring
    # it was proven to work in our spikes.
    # It's been observed to come through as lowercase, so fallback if not present
    sleep_time_str = resp.headers.get('Retry-After', resp.headers.get('retry-after'))
    yield math.floor(float(sleep_time_str))

def shopify_error_handling():
    def decorator(fnc):
        @functools.wraps(fnc)
        def wrapped(*args, **kwargs):
            # Shopify returns 429s when their leaky bucket rate limiting
            # algorithm has been tripped. This will retry those
            # indefinitely. At some point we could consider adding a
            # max_retry configuration into this loop. So far that has
            # proven unnecessary
            attempt = 0
            i = 0
            while i < 1000: #arbitrary big number
                try:
                    return fnc(*args, **kwargs)
                except pyactiveresource.connection.ClientError as client_error:
                    # We have never seen this be anything _but_ a 429. Other
                    # states should be consider untested.
                    resp = client_error.response
                    if resp.code == 429:
                        # Retry-After is an undocumented header. But honoring
                        # it was proven to work in our spikes.
                        sleep_time_str = resp.headers.get('Retry-After') \
                                         or resp.headers.get('retry-after', 2)
                        LOGGER.info("Received 429 -- sleeping for %s seconds", sleep_time_str)
                        time.sleep(math.floor(float(sleep_time_str)))
                        i += 1
                        continue
                    elif (resp.code == 500 or 599) and (attempt<MAX_RETRIES):
                        attempt += 1
                        continue
                    else:
                        LOGGER.error("Received a %s error.", resp.code)
                        raise
        return wrapped
    return decorator

class Error(Exception):
    """Base exception for the API interaction module"""

class OutOfOrderIdsError(Error):
    """Raised if our expectation of ordering by ID is violated"""

class Stream():
    # Used for bookmarking and stream identification. Is overridden by
    # subclasses to change the bookmark key.
    name = None
    replication_method = 'INCREMENTAL'
    replication_key = 'updated_at'
    key_properties = ['id']
    # Controls which SDK object we use to call the API by default.
    replication_object = None

    def get_bookmark(self):
        bookmark = (singer.get_bookmark(Context.state,
                                        # name is overridden by some substreams
                                        self.name,
                                        self.replication_key)
                    or Context.config["start_date"])
        return utils.strptime_with_tz(bookmark)

    def get_since_id(self):
        return singer.get_bookmark(Context.state,
                                   # name is overridden by some substreams
                                   self.name,
                                   'since_id')

    def update_bookmark(self, bookmark_value, bookmark_key=None):
        # NOTE: Bookmarking can never be updated to not get the most
        # recent thing it saw the next time you run, because the querying
        # only allows greater than or equal semantics.
        singer.write_bookmark(
            Context.state,
            # name is overridden by some substreams
            self.name,
            bookmark_key or self.replication_key,
            bookmark_value
        )
        singer.write_state(Context.state)


    # This function can be overridden by subclasses for specialized API
    # interactions. If you override it you need to remember to decorate it
    # with shopify_error_handling to get 429 and 500 handling.
    @shopify_error_handling()
    def call_api(self, query_params):
        return self.replication_object.find(**query_params)

    def get_objects(self):
        updated_at_min = self.get_bookmark()

        stop_time = singer.utils.now().replace(microsecond=0)
        date_window_size = float(Context.config.get("date_window_size", DATE_WINDOW_SIZE))
        results_per_page = int(Context.config.get("results_per_page", RESULTS_PER_PAGE))

        # Page through till the end of the resultset
        while updated_at_min < stop_time:
            # Bookmarking can also occur on the since_id
            since_id = self.get_since_id() or 1

            if since_id != 1:
                LOGGER.info("Resuming sync from since_id %d", since_id)

            # It's important that `updated_at_min` has microseconds
            # truncated. Why has been lost to the mists of time but we
            # think it has something to do with how the API treats
            # microseconds on its date windows. Maybe it's possible to
            # drop data due to rounding errors or something like that?
            updated_at_max = updated_at_min + datetime.timedelta(days=date_window_size)
            if updated_at_max > stop_time:
                updated_at_max = stop_time
            while True:
                query_params = {
                    "since_id": since_id,
                    "updated_at_min": updated_at_min,
                    "updated_at_max": updated_at_max,
                    "limit": results_per_page,
                    "status": "any"
                }
                objects = self.call_api(query_params)
                for obj in objects:
                    if obj.id < since_id:
                        # This verifies the api behavior expectation we
                        # have that all results actually honor the
                        # since_id parameter.
                        raise OutOfOrderIdsError("obj.id < since_id: {} < {}".format(
                            obj.id, since_id))
                    yield obj

                # You know you're at the end when the current page has
                # less than the request size limits you set.
                if len(objects) < results_per_page:
                    # Save the updated_at_max as our bookmark as we've synced all rows up in our
                    # window and can move forward. Also remove the since_id because we want to
                    # restart at 1.
                    Context.state.get('bookmarks', {}).get(self.name, {}).pop('since_id', None)
                    self.update_bookmark(utils.strftime(updated_at_max))
                    break

                if objects[-1].id != max([o.id for o in objects]):
                    # This verifies the api behavior expectation we have
                    # that all pages are internally ordered by the
                    # `since_id`.
                    raise OutOfOrderIdsError("{} is not the max id in objects ({})".format(
                        objects[-1].id, max([o.id for o in objects])))
                since_id = objects[-1].id

                # Put since_id into the state.
                self.update_bookmark(since_id, bookmark_key='since_id')

            updated_at_min = updated_at_max

    def sync(self):
        """Yield's processed SDK object dicts to the caller.

        This is the default implementation. Get's all of self's objects
        and calls to_dict on them with no further processing.
        """
        for obj in self.get_objects():
            yield obj.to_dict()
