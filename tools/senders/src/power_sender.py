import os
import sys
import time
import yaml
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

from src.apptainer.ApptainerHandler import ApptainerHandler
from src.utils.MyUtils import create_dir, clean_log_file

POLLING_FREQUENCY = 5

SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
SENDERS_DIR = os.path.dirname(SCRIPT_DIR)
INFLUXDB_CONFIG_FILE = f"{SENDERS_DIR}/influxdb/config.yml"
LOG_DIR = f"{SENDERS_DIR}/log"
LOG_FILE = f"{LOG_DIR}/power_sender.log"


class PowerSender:

    output_file = None
    last_read_position = None
    start_timestamp = None
    logger = None
    smartwatts_output = None
    apptainer_handler = None
    influxdb_bucket = None
    influxdb_client = None

    @staticmethod
    def init_logging_config():
        create_dir(LOG_DIR)
        clean_log_file(LOG_DIR, LOG_FILE)
        logging.basicConfig(filename=LOG_FILE,
                            level=logging.INFO,
                            format='%(levelname)s (%(name)s): %(asctime)s %(message)s')

    def __get_influxdb_session(self):
        with open(INFLUXDB_CONFIG_FILE, "r") as f:
            influxdb_config = yaml.load(f, Loader=yaml.FullLoader)

        # Get session to InfluxDB
        influxdb_url = f"http://{influxdb_config['influxdb_host']}:8086"
        self.influxdb_client = InfluxDBClient(url=influxdb_url, token=influxdb_config['influxdb_token'], org=influxdb_config['influxdb_org'])

    def __init__(self, smartwatts_output, influxdb_bucket):
        self.output_file = {}
        self.last_read_position = {}
        self.start_timestamp = datetime.now(timezone.utc)
        self.logger = logging.getLogger("power_sender")
        self.smartwatts_output = smartwatts_output
        self.apptainer_handler = ApptainerHandler(privileged=True)
        self.influxdb_bucket = influxdb_bucket
        self.__get_influxdb_session()

    def aggregate_and_send_data(self, bulk_data, host):
        # Aggregate data by cpu (mean) and timestamp (sum)
        if len(bulk_data) > 0:
            agg_data = pd.DataFrame(bulk_data).groupby(['timestamp', 'cpu']).agg({'value': 'mean'}).reset_index().groupby('timestamp').agg({'value': 'sum'}).reset_index()

            # Format data to InfluxDB line protocol
            target_metrics = []
            for _, row in agg_data .iterrows():
                data = f"power,host={host} value={row['value']} {int(row['timestamp'].timestamp() * 1e9)}"
                target_metrics.append(data)

            # Send data to InfluxDB
            try:
                self.influxdb_client.write_api(write_options=SYNCHRONOUS).write(bucket=influxdb_bucket, record=target_metrics)
            except Exception as e:
                self.logger.error(f"Error sending data to InfluxDB: {e}")

    def get_data_from_lines(self, lines, target):
        bulk_data = []
        for line in lines:
            # line example: <timestamp> <sensor> <target> <value> ...
            fields = line.strip().split(',')
            num_fields = len(fields)
            if num_fields < 4:
                raise Exception(f"Missing some fields in SmartWatts output for "
                                f"target {target} ({num_fields} out of 4 expected fields)")

            # SmartWatts timestamps are 2 hours ahead from UTC (UTC-02:00)
            # Normalize timestamps to UTC (actually UTC-02:00) and add 2 hours to get real UTC
            data = {
                "timestamp": datetime.fromtimestamp(int(fields[0]) / 1000, timezone.utc) + timedelta(hours=2),
                "value": float(fields[3]),
                "cpu": int(fields[4])
            }

            # Only data obtained after the start of this program are sent
            if data["timestamp"] < self.start_timestamp:
                continue
            bulk_data.append(data)
        return bulk_data

    def read_target_output(self, output_path, current_position, target):
        num_lines = 0
        new_position = None
        try:
            if os.path.isfile(output_path) and os.access(output_path, os.R_OK):
                # Read target output
                with open(output_path, 'r') as file:
                    # If file is empty, skip
                    if os.path.getsize(output_path) <= 0:
                        self.logger.warning(f"Target {target} file is empty: {output_path}")
                        return num_lines, new_position

                    # Go to last read position
                    file.seek(current_position)

                    # Skip header
                    if current_position == 0:
                        next(file)

                    lines = file.readlines()
                    num_lines = len(lines)
                    if num_lines == 0:
                        self.logger.warning(f"There aren't new lines to process for target {target}")
                        return num_lines, new_position

                    new_position = file.tell()

                    # Gather data from target output
                    bulk_data = self.get_data_from_lines(lines, target)

                    # Aggregate data by cpu (mean) and timestamp (sum)
                    self.aggregate_and_send_data(bulk_data, target)
            else:
                self.logger.error(f"Couldn't access file: {output_path}")

        except IOError as e:
            self.logger.error(f"Error while reading {output_path}: {str(e)}")

        finally:
            return num_lines, new_position

    def process_containers(self):

        iter_count = {"targets": 0, "lines": 0}

        for container in self.apptainer_handler.get_running_containers_list():

            cont_pid = container["pid"]
            cont_name = container["name"]

            # If target is not registered, initialize it
            if cont_pid not in self.output_file:
                self.logger.info(f"Found new target with name {cont_name} and pid {cont_pid}. Registered.")
                self.output_file[cont_pid] = f"{self.smartwatts_output}/sensor-apptainer-{cont_pid}/PowerReport.csv"
                self.last_read_position[cont_pid] = 0

            if not os.path.isfile(self.output_file[cont_pid]) or not os.access(self.output_file[cont_pid], os.R_OK):
                self.logger.warning(f"Couldn't access file from target {container['name']}: {self.output_file[cont_pid]}")
                continue

            processed_lines, new_position = self.read_target_output(self.output_file[cont_pid],
                                                                    self.last_read_position[cont_pid],
                                                                    cont_name)

            if processed_lines > 0:
                iter_count["targets"] += 1
                iter_count["lines"] += processed_lines

            if new_position is not None:
                self.last_read_position[cont_pid] = new_position

        return iter_count

    def process_global_power(self):

        iter_count = {"targets": 0, "lines": 0}
        output_file =
        for target in ["rapl", "global"]:
            output_file = f"{self.smartwatts_output}/sensor-{target}/PowerReport.csv"
            cont_pid = container["pid"]
            cont_name = container["name"]

            # If target is not registered, initialize it
            if cont_pid not in self.output_file:
                self.logger.info(f"Found new target with name {cont_name} and pid {cont_pid}. Registered.")
                self.output_file[cont_pid] = f"{smartwatts_output}/sensor-apptainer-{cont_pid}/PowerReport.csv"
                self.last_read_position[cont_pid] = 0

            if not os.path.isfile(self.output_file[cont_pid]) or not os.access(self.output_file[cont_pid], os.R_OK):
                self.logger.warning(f"Couldn't access file from target {container['name']}: {self.output_file[cont_pid]}")
                continue

            processed_lines, new_position = self.read_target_output(self.output_file[cont_pid],
                                                                    self.last_read_position[cont_pid],
                                                                    cont_name)

            if processed_lines > 0:
                iter_count["targets"] += 1
                iter_count["lines"] += processed_lines

            if new_position is not None:
                self.last_read_position[cont_pid] = new_position

        return iter_count

    def send_power(self):

        # Create log files and set logger
        self.init_logging_config()

        # Log current timestamp UTC
        self.logger.info(f"Start time: {self.start_timestamp}")

        # Read SmartWatts output and get targets
        while True:
            try:
                t_start = time.perf_counter_ns()

                iter_count = self.process_containers()

                t_stop = time.perf_counter_ns()
                delay = (t_stop - t_start) / 1e9
                self.logger.info(f"Processed {iter_count['targets']} targets and {iter_count['lines']} "
                                 f"lines causing a delay of {delay} seconds")

                # Avoids negative sleep times when there is a high delay
                if delay > POLLING_FREQUENCY:
                    self.logger.warning(f"High delay ({delay}) causing negative sleep times. "
                                        f"Waiting until the next {POLLING_FREQUENCY}s cycle")
                    delay = delay % POLLING_FREQUENCY
                time.sleep(POLLING_FREQUENCY - delay)

            except Exception as e:
                self.logger.error(f"Unexpected error: {str(e)}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise Exception("Missing some arguments: power_sender.py <INFLUXDB_BUCKET> <SMARTWATTS_OUTPUT>")

    influxdb_bucket = sys.argv[1]
    smartwatts_output = sys.argv[2]

    try:
        power_sender = PowerSender(smartwatts_output, influxdb_bucket)
        power_sender.send_power()
    except Exception as e:
        raise Exception(f"Error while trying to create PowerSender instance: {str(e)}")
