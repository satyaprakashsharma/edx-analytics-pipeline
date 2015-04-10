
from collections import namedtuple
import hashlib
import datetime
import logging
import math
import xml.etree.cElementTree as ET
import urllib

import ciso8601
import luigi

from edx.analytics.tasks.mapreduce import MapReduceJobTask, MapReduceJobTaskMixin
from edx.analytics.tasks.pathutil import EventLogSelectionMixin, EventLogSelectionDownstreamMixin
from edx.analytics.tasks.url import get_target_from_url, url_path_join, ExternalURL
from edx.analytics.tasks.util import eventlog, opaque_key_util
from edx.analytics.tasks.util.hive import WarehouseMixin, HivePartition, HiveTableTask, HiveQueryToMysqlTask
from edx.analytics.tasks.mysql_load import MysqlInsertTask

log = logging.getLogger(__name__)


VIDEO_PLAYED = 'play_video'
VIDEO_PAUSED = 'pause_video'
VIDEO_EVENT_TYPES = frozenset([
    VIDEO_PLAYED,
    VIDEO_PAUSED,
])
VIDEO_SESSION_END_INDICATORS = frozenset([
    'seq_next',
    'seq_prev',
    'seq_goto',
    'page_close',
])
VIDEO_SESSION_THRESHOLD = 60
VIDEO_SESSION_UNKNOWN_DURATION = -1


VideoSession = namedtuple('VideoSession', [
    'session_id', 'start_timestamp', 'encoded_module_id', 'start_offset', 'video_duration'])


class UserVideoSessionTask(EventLogSelectionMixin, MapReduceJobTask):

    output_root = luigi.Parameter()

    def mapper(self, line):
        value = self.get_event_and_date_string(line)
        if value is None:
            return
        event, _date_string = value

        username = event.get('username')
        if username is None:
            log.error("encountered event with no username: %s", event)
            return

        if len(username) == 0:
            return

        event_type = event.get('event_type')
        if event_type is None:
            log.error("encountered event with no event_type: %s", event)
            return

        timestamp = eventlog.get_event_time_string(event)
        if timestamp is None:
            log.error("encountered event with bad timestamp: %s", event)
            return

        encoded_module_id = None
        current_time = None
        youtube_id = None
        if event_type in VIDEO_EVENT_TYPES:
            event_data = eventlog.get_event_data(event)
            encoded_module_id = event_data.get('id')
            current_time = event_data.get('currentTime')
            if event_type == 'play_video':
                code = event_data.get('code')
                if code != 'html5' and code != 'mobile':
                    youtube_id = code
        elif event_type in VIDEO_SESSION_END_INDICATORS:
            pass
        else:
            return

        yield (username, (timestamp, event_type, encoded_module_id, current_time, youtube_id))

    def reducer(self, username, events):
        sorted_events = sorted(events)
        session = None
        video_durations = {}
        for event in sorted_events:
            timestamp, event_type, encoded_module_id, current_time, youtube_id = event
            parsed_timestamp = ciso8601.parse_datetime(timestamp)
            if current_time:
                current_time = float(current_time)

            def start_session():
                m = hashlib.md5()
                m.update(username)
                m.update(encoded_module_id)
                m.update(timestamp)

                video_duration = VIDEO_SESSION_UNKNOWN_DURATION
                if youtube_id:
                    video_duration = video_durations.get(youtube_id)
                    if not video_duration:
                        video_duration = self.get_video_duration(youtube_id)
                        video_durations[youtube_id] = video_duration

                return VideoSession(
                    session_id=m.hexdigest(),
                    start_timestamp=parsed_timestamp,
                    encoded_module_id=encoded_module_id,
                    start_offset=current_time,
                    video_duration=video_duration
                )

            def end_session(end_time):
                if end_time is None or session.start_offset is None:
                    log.error(
                        'Invalid session ending, %s, %s, %f, %f',
                        username, str(event), end_time, session.start_offset
                    )
                    return None

                if end_time > session.video_duration:
                    return None

                if (end_time - session.start_offset) < 0.5:
                    return None
                else:
                    return (
                        username,
                        session.session_id,
                        session.encoded_module_id,
                        session.video_duration,
                        session.start_timestamp.isoformat(),
                        session.start_offset,
                        end_time,
                        event_type,
                    )

            if session:
                if event_type == VIDEO_PLAYED:
                    time_diff = parsed_timestamp - session.start_timestamp
                    if time_diff < datetime.timedelta(seconds=0.5):
                        # log.warn(
                        #     'Play video events detected %f seconds apart, second one is ignored.',
                        #     time_diff.total_seconds()
                        # )
                        continue
                    else:
                        record = end_session(current_time)
                        if record:
                            yield record

                    session = start_session()

                elif event_type == VIDEO_PAUSED:
                    record = end_session(current_time)
                    if record:
                        yield record
                    session = None

                elif event_type in VIDEO_SESSION_END_INDICATORS:
                    session_length = (parsed_timestamp - session.start_timestamp).total_seconds()
                    session_length = min(session_length, VIDEO_SESSION_THRESHOLD)
                    session_end = session.start_offset + session_length
                    record = end_session(session_end)
                    if record:
                        yield record
                    session = None
            else:
                if event_type == VIDEO_PLAYED:
                    session = start_session()

    def output(self):
        return get_target_from_url(self.output_root)

    def get_video_duration(self, youtube_id):
        duration = VIDEO_SESSION_UNKNOWN_DURATION
        try:
            f = urllib.urlopen("http://gdata.youtube.com/feeds/api/videos/{0}".format(youtube_id))
            xml_string = f.read()
            tree = ET.fromstring(xml_string)
            for item in tree.iter('{http://gdata.youtube.com/schemas/2007}duration'):
                if 'seconds' in item.attrib:
                    duration = int(item.attrib['seconds'])
        except IOError:
            pass
        except ET.ParseError:
            pass
        finally:
            f.close()

        return duration


class VideoTableDownstreamMixin(WarehouseMixin, EventLogSelectionDownstreamMixin, MapReduceJobTaskMixin):
    pass


class UserVideoSessionTableTask(VideoTableDownstreamMixin, HiveTableTask):

    @property
    def table(self):
        return 'user_video_session'

    @property
    def columns(self):
        return [
            ('username', 'STRING'),
            ('video_session_id', 'STRING'),
            ('encoded_module_id', 'STRING'),
            ('start_timestamp', 'STRING'),
            ('start_offset', 'FLOAT'),
            ('end_offset', 'FLOAT'),
        ]

    @property
    def partition(self):
        return HivePartition('dt', self.interval.date_b.isoformat())  # pylint: disable=no-member

    def requires(self):
        return UserVideoSessionTask(
            mapreduce_engine=self.mapreduce_engine,
            n_reduce_tasks=self.n_reduce_tasks,
            source=self.source,
            interval=self.interval,
            pattern=self.pattern,
            output_root=self.partition_location,
        )

    def output(self):
        return self.requires().output()


class VideoUsageTask(EventLogSelectionDownstreamMixin, WarehouseMixin, MapReduceJobTask):

    output_root = luigi.Parameter()
    input_path = luigi.Parameter(default=None)

    def requires(self):
        if self.input_path:
            return ExternalURL(self.input_path)
        else:
            return UserVideoSessionTableTask(
                mapreduce_engine=self.mapreduce_engine,
                n_reduce_tasks=self.n_reduce_tasks,
                source=self.source,
                interval=self.interval,
                pattern=self.pattern,
                warehouse_path=self.warehouse_path
            )

    def mapper(self, line):
        username, session_id, encoded_module_id, start_timestamp_str, start_offset, end_offset = line.split('\t')
        yield (encoded_module_id, (username, start_offset, end_offset))

    def reducer_backup(self, encoded_module_id, sessions):
        usage_map = {}

        for session in sessions:
            username, start_offset, end_offset = session
            for second in xrange(int(math.floor(float(start_offset))), int(math.ceil(float(end_offset))), 1):
                stats = usage_map.setdefault(second, {})
                users = stats.setdefault('users', set())
                users.add(username)
                stats['views'] = stats.get('views', 0) + 1

        csv_data = []
        for second in usage_map.keys():
            stats = usage_map[second]
            csv_data.append(','.join([str(x) for x in (second, len(stats['users']), stats['views'])]))

        blob_data = '|'.join(csv_data)
        log.warn(str(len(blob_data)))
        yield encoded_module_id, blob_data

    def reducer(self, encoded_module_id, sessions):
        usage_map = {}

        for session in sessions:
            username, start_offset, end_offset = session
            for second in xrange(int(math.floor(float(start_offset))), int(math.ceil(float(end_offset))), 1):
                stats = usage_map.setdefault(second, {})
                users = stats.setdefault('users', set())
                users.add(username)
                stats['views'] = stats.get('views', 0) + 1

        for second in usage_map.keys():
            stats = usage_map[second]
            yield encoded_module_id, second, len(stats['users']), stats['views']
            del usage_map[second]

    def output(self):
        return get_target_from_url(self.output_root)


class VideoUsageTableTask(VideoTableDownstreamMixin, HiveTableTask):

    @property
    def table(self):
        return 'video_usage'

    @property
    def columns(self):
        return [
            ('module_id', 'STRING'),
            ('segment', 'INT'),
            ('num_users', 'INT'),
            ('num_views', 'INT'),
        ]

    @property
    def partition(self):
        return HivePartition('dt', self.interval.date_b.isoformat())  # pylint: disable=no-member

    def requires(self):
        return VideoUsageTask(
            mapreduce_engine=self.mapreduce_engine,
            n_reduce_tasks=self.n_reduce_tasks,
            source=self.source,
            interval=self.interval,
            pattern=self.pattern,
            warehouse_path=self.warehouse_path,
            output_root=self.partition_location
        )

    def output(self):
        return self.requires().output()


class InsertToMysqlVideoUsageTask(VideoTableDownstreamMixin, MysqlInsertTask):

    overwrite = luigi.BooleanParameter(default=True)

    @property
    def table(self):
        return "video_usage"

    @property
    def columns(self):
        return [
            ('module_id', 'VARCHAR(255)'),
            ('segment', 'INTEGER'),
            ('num_users', 'INTEGER'),
            ('num_views', 'INTEGER'),
        ]

    @property
    def insert_source_task(self):
        return VideoUsageTableTask(
            mapreduce_engine=self.mapreduce_engine,
            n_reduce_tasks=self.n_reduce_tasks,
            source=self.source,
            interval=self.interval,
            pattern=self.pattern,
            warehouse_path=self.warehouse_path,
        )
