import sys
import os
import argparse
import numpy as np
import time
import base64
import logging
import json

from clipper_admin import ClipperConnection, DockerContainerManager
from threading import Lock
from datetime import datetime
from io import BytesIO
from PIL import Image
from containerized_utils.zmq_client import Client
from containerized_utils import driver_utils
from multiprocessing import Process, Queue

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
    datefmt='%y-%m-%d:%H:%M:%S',
    level=logging.INFO)

logger = logging.getLogger(__name__)

# Models and applications for each heavy node
# will share the same name
INCEPTION_FEATS_MODEL_APP_NAME = "inception"
TF_KERNEL_SVM_MODEL_APP_NAME = "tf-kernel-svm"
TF_LOG_REG_MODEL_APP_NAME = "tf-log-reg"
TF_RESNET_MODEL_APP_NAME = "tf-resnet-feats"

INCEPTION_FEATS_IMAGE_NAME = "model-comp/inception-feats"
TF_KERNEL_SVM_IMAGE_NAME = "model-comp/tf-kernel-svm"
TF_LOG_REG_IMAGE_NAME = "model-comp/tf-log-reg"
TF_RESNET_IMAGE_NAME = "model-comp/tf-resnet-feats"

CLIPPER_ADDRESS = "localhost"
CLIPPER_SEND_PORT = 4456
CLIPPER_RECV_PORT = 4455

DEFAULT_OUTPUT = "TIMEOUT"

########## Setup ##########

def setup_clipper(configs):
    cl = ClipperConnection(DockerContainerManager(redis_port=6380))
    cl.stop_all()
    cl.start_clipper(
        query_frontend_image="clipper/zmq_frontend:develop",
        redis_cpu_str="0",
        mgmt_cpu_str="0",
        query_cpu_str="1-8")
    time.sleep(10)
    for config in configs:
        driver_utils.setup_heavy_node(cl, config, DEFAULT_OUTPUT)
    time.sleep(20)
    logger.info("Clipper is set up!")
    return config

def setup_inception(batch_size,
                    num_replicas,
                    cpus_per_replica,
                    allocated_cpus,
                    allocated_gpus):

    return driver_utils.HeavyNodeConfig(name=INCEPTION_FEATS_MODEL_APP_NAME,
                                        input_type="floats",
                                        model_image=INCEPTION_FEATS_IMAGE_NAME,
                                        allocated_cpus=allocated_cpus,
                                        cpus_per_replica=cpus_per_replica,
                                        gpus=allocated_gpus,
                                        batch_size=batch_size,
                                        num_replicas=num_replicas,
                                        use_nvidia_docker=True,
                                        no_diverge=True,
                                        )

def setup_log_reg(batch_size,
                  num_replicas,
                  cpus_per_replica,
                  allocated_cpus,
                  allocated_gpus):

    return driver_utils.HeavyNodeConfig(name=TF_LOG_REG_MODEL_APP_NAME,
                                        input_type="floats",
                                        model_image=TF_LOG_REG_IMAGE_NAME,
                                        allocated_cpus=allocated_cpus,
                                        cpus_per_replica=cpus_per_replica,
                                        gpus=allocated_gpus,
                                        batch_size=batch_size,
                                        num_replicas=num_replicas,
                                        use_nvidia_docker=True,
                                        no_diverge=True,
                                        )

def setup_kernel_svm(batch_size,
                    num_replicas,
                    cpus_per_replica,
                    allocated_cpus,
                    allocated_gpus):

    return driver_utils.HeavyNodeConfig(name=TF_KERNEL_SVM_MODEL_APP_NAME,
                                        input_type="floats",
                                        model_image=TF_KERNEL_SVM_IMAGE_NAME,
                                        allocated_cpus=allocated_cpus,
                                        cpus_per_replica=cpus_per_replica,
                                        gpus=allocated_gpus,
                                        batch_size=batch_size,
                                        num_replicas=num_replicas,
                                        use_nvidia_docker=True,
                                        no_diverge=True,
                                        )

def setup_resnet(batch_size,
                 num_replicas,
                 cpus_per_replica,
                 allocated_cpus,
                 allocated_gpus):

    return driver_utils.HeavyNodeConfig(name=TF_RESNET_MODEL_APP_NAME,
                                        input_type="floats",
                                        model_image=TF_RESNET_IMAGE_NAME,
                                        allocated_cpus=allocated_cpus,
                                        cpus_per_replica=cpus_per_replica,
                                        gpus=allocated_gpus,
                                        batch_size=batch_size,
                                        num_replicas=num_replicas,
                                        use_nvidia_docker=True,
                                        no_diverge=True,
                                        )


########## Benchmarking ##########

def get_batch_sizes(metrics_json):
    hists = metrics_json["histograms"]
    mean_batch_sizes = {}
    for h in hists:
        if "batch_size" in h.keys()[0]:
            name = h.keys()[0]
            model = name.split(":")[1]
            mean = h[name]["mean"]
            mean_batch_sizes[model] = round(float(mean), 2)
    return mean_batch_sizes

def get_queue_sizes(metrics_json):
    hists = metrics_json["histograms"]
    mean_queue_sizes = {}
    for h in hists:
        if "queue_size" in h.keys()[0]:
            name = h.keys()[0]
            model = name.split(":")[1]
            mean = h[name]["mean"]
            mean_queue_sizes[model] = round(float(mean), 2)

    return mean_queue_sizes

class Predictor(object):

    def __init__(self, clipper_metrics, trial_length):
        self.trial_length = trial_length
        self.outstanding_reqs = {}
        self.client = Client(CLIPPER_ADDRESS, CLIPPER_SEND_PORT, CLIPPER_RECV_PORT)
        self.client.start()
        self.init_stats()
        self.stats = {
            "thrus": [],
            "p99_lats": [],
            "all_lats": [],
            "mean_lats": []}
        self.total_num_complete = 0
        self.cl = ClipperConnection(DockerContainerManager(redis_port=6380))
        self.cl.connect()
        self.get_clipper_metrics = clipper_metrics
        if self.get_clipper_metrics:
            self.stats["all_metrics"] = []
            self.stats["mean_batch_sizes"] = []

    def init_stats(self):
        self.latencies = []
        self.batch_num_complete = 0
        self.cur_req_id = 0
        self.start_time = datetime.now()

    def print_stats(self):
        lats = np.array(self.latencies)
        p99 = np.percentile(lats, 99)
        mean = np.mean(lats)
        end_time = datetime.now()
        thru = float(self.batch_num_complete) / (end_time - self.start_time).total_seconds()
        self.stats["thrus"].append(thru)
        self.stats["p99_lats"].append(p99)
        self.stats["all_lats"].append(lats)
        self.stats["mean_lats"].append(mean)
        if self.get_clipper_metrics:
            metrics = self.cl.inspect_instance()
            batch_sizes = get_batch_sizes(metrics)
            queue_sizes = get_queue_sizes(metrics)
            self.stats["mean_batch_sizes"].append(batch_sizes)
            self.stats["all_metrics"].append(metrics)
            logger.info(("p99: {p99}, mean: {mean}, thruput: {thru}, "
                         "batch_sizes: {batches} queue_sizes: {queues}").format(p99=p99, mean=mean, thru=thru,
                                                          batches=json.dumps(
                                                              batch_sizes, sort_keys=True)))
        else:
            logger.info("p99: {p99}, mean: {mean}, thruput: {thru}".format(p99=p99,
                                                                           mean=mean,
                                                                           thru=thru))

    def predict(self, resnet_input, inception_input):
        begin_time = datetime.now()
        classifications_lock = Lock()
        classifications = {}

        def update_perf_stats():
            end_time = datetime.now()
            latency = (end_time - begin_time).total_seconds()
            self.latencies.append(latency)
            self.total_num_complete += 1
            self.batch_num_complete += 1
            if self.batch_num_complete % self.trial_length == 0:
                self.print_stats()
                self.init_stats()

        def resnet_feats_continuation(resnet_features):
            if resnet_features == DEFAULT_OUTPUT:
                return
            return self.client.send_request(TF_KERNEL_SVM_MODEL_APP_NAME, resnet_features)

        def svm_continuation(svm_classification):
            if svm_classification == DEFAULT_OUTPUT:
                return
            else:
                classifications_lock.acquire()
                if TF_LOG_REG_MODEL_APP_NAME not in classifications:
                    classifications[TF_KERNEL_SVM_MODEL_APP_NAME] = svm_classification
                else:
                    update_perf_stats()
                classifications_lock.release()

        def inception_feats_continuation(inception_features):
            if inception_features == DEFAULT_OUTPUT:
                return
            return self.client.send_request(TF_LOG_REG_MODEL_APP_NAME, inception_features)


        def log_reg_continuation(log_reg_vals):
            if log_reg_vals == DEFAULT_OUTPUT:
                return
            else:
                classifications_lock.acquire()
                if TF_KERNEL_SVM_MODEL_APP_NAME not in classifications:
                    classifications[TF_LOG_REG_MODEL_APP_NAME] = log_reg_vals
                else:
                    update_perf_stats()
                classifications_lock.release()

        self.client.send_request(TF_RESNET_MODEL_APP_NAME, resnet_input) \
            .then(resnet_feats_continuation) \
            .then(svm_continuation)

        self.client.send_request(INCEPTION_FEATS_MODEL_APP_NAME, inception_input) \
            .then(inception_feats_continuation) \
            .then(log_reg_continuation)

class DriverBenchmarker(object):
    def __init__(self, configs, queue, client_num, latency_upper_bound):
        self.configs = configs
        self.queue = queue
        assert client_num == 0
        self.client_num = client_num
        logger.info("Generating random inputs")
        base_inputs = [(self._get_resnet_input(), self._get_inception_input()) for _ in range(1000)]
        self.inputs = [i for _ in range(40) for i in base_inputs]
        self.latency_upper_bound = latency_upper_bound

    def run(self):
        self.initialize_request_rate()
        self.find_steady_state()
        return

    # start with an overly aggressive request rate
    # then back off
    def initialize_request_rate(self):
        # initialize delay to be very small
        self.delay = 0.001
        setup_clipper(self.configs)
        time.sleep(5)
        predictor = Predictor(clipper_metrics=True)
        idx = 0
        while len(predictor.stats["thrus"]) < 6:
            predictor.predict(input_item=self.inputs[idx])
            time.sleep(self.delay)
            idx += 1
            idx = idx % len(self.inputs)

        max_thruput = np.mean(predictor.stats["thrus"][1:])
        self.delay = 1.0 / max_thruput
        logger.info("Initializing delay to {}".format(self.delay))

    def increase_delay(self):
        if self.delay < 0.005:
            self.delay += 0.0002
        elif self.delay < 0.01:
            self.delay += 0.0005
        else:
            self.delay += 0.001


    def find_steady_state(self):
        setup_clipper(self.configs)
        time.sleep(7)
        predictor = Predictor(clipper_metrics=True)
        idx = 0
        done = False
        # start checking for steady state after 7 trials
        last_checked_length = 6
        while not done:
            predictor.predict(input_item=self.inputs[idx])
            time.sleep(self.delay)
            idx += 1
            idx = idx % len(self.inputs)

            if len(predictor.stats["thrus"]) > last_checked_length:
                last_checked_length = len(predictor.stats["thrus"]) + 4
                convergence_state = driver_utils.check_convergence(predictor.stats, self.configs, self.latency_upper_bound)
                # Diverging, try again with higher
                # delay
                if convergence_state == INCREASING or convergence_state == CONVERGED_HIGH:
                    self.increase_delay()
                    logger.info("Increasing delay to {}".format(self.delay))
                    done = True
                    return self.find_steady_state()
                elif convergence_state == CONVERGED:
                    logger.info("Converged with delay of {}".format(self.delay))
                    done = True
                    self.queue.put(predictor.stats)
                    return
                elif len(predictor.stats) > 100:
                    self.increase_delay()
                    logger.info("Increasing delay to {}".format(self.delay))
                    done = True
                    return self.find_steady_state()
                elif convergence_state == DECREASING or convergence_state == UNKNOWN:
                    logger.info("Not converged yet. Still waiting")
                else:
                    logger.error("Unknown convergence state: {}".format(convergence_state))
                    sys.exit(1)

    def _get_resnet_input(self):
        resnet_input = np.array(np.random.rand(224, 224, 3) * 255, dtype=np.float32)
        return resnet_input.flatten()

    def _get_inception_input(self):
        inception_input = np.array(np.random.rand(299, 299, 3) * 255, dtype=np.float32)
        return inception_input.flatten()

class RequestDelayConfig:
    def __init__(self, request_delay):
        self.request_delay = request_delay
        
    def to_json(self):
        return json.dumps(self.__dict__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Set up and benchmark models for Clipper image driver 1')
    parser.add_argument('-t', '--num_trials', type=int, default=30, help='The number of trials to complete for the benchmarking process')
    parser.add_argument('-b', '--batch_sizes', type=int, nargs='+', help="The batch size configurations to benchmark for the model. Each configuration will be benchmarked separately.")
    parser.add_argument('-c', '--model_cpus', type=int, nargs='+', help="The set of cpu cores on which to run replicas of the provided model")
    parser.add_argument('-rd', '--request_delay', type=float, default=.015, help="The delay, in seconds, between requests")
    parser.add_argument('-l', '--trial_length', type=int, default=10, help="The length of each trial, in number of requests")
    parser.add_argument('-n', '--num_clients', type=int, default=1, help='number of clients')

    args = parser.parse_args()

    queue = Queue()

    ## THIS IS FOR 500MS
    ## FORMAT IS (INCEPTION, LOG_REG, KSVM, RESNET)
    five_hundred_ms_reps = [(1, 1, 1, 1),
                            (1, 1, 1, 2),
                            (1, 1, 1, 3),
                            (2, 1, 1, 3),
                            (2, 1, 1, 4),
                            (2, 1, 1, 5),
                            (3, 1, 1, 5)]

    ## THIS IS FOR 500MS
    ## FORMAT IS (INCEPTION, LOG_REG, KSVM, RESNET)
    five_hundred_ms_batches = (10, 2, 16, 6)

    five_hundred_ms_latency_upper_bound = 1.500

    ## THIS IS FOR 375MS
    ## FORMAT IS (INCEPTION, LOG_REG, KSVM, RESNET)
    three_seven_five_ms_reps = [(1, 1, 1, 1),
                                (1, 1, 1, 2),
                                (1, 1, 1, 3),
                                (1, 1, 1, 4),
                                (1, 1, 1, 5),
                                (2, 1, 1, 5),
                                (2, 1, 1, 6)]

    ## THIS IS FOR 375MS
    ## FORMAT IS (INCEPTION, LOG_REG, KSVM, RESNET)
    three_seven_five_ms_batches = (7, 2, 9, 2)

    three_seven_five_ms_latency_upper_bound = 1.000

    ## THIS IS FOR 375MS
    ## FORMAT IS (INCEPTION, LOG_REG, KSVM, RESNET)
    thousand_ms_reps = [(1, 1, 1, 1),
                        (1, 1, 1, 2),
                        (2, 1, 1, 2),
                        (2, 1, 1, 3),
                        (2, 1, 1, 4),
                        (3, 1, 1, 4),
                        (3, 1, 1, 5)]

    ## THIS IS FOR 1000MS
    ## FORMAT IS (INCEPTION, LOG_REG, KSVM, RESNET)
    thousand_ms_batches = (16, 2, 16, 15)

    thousand_ms_latency_upper_bound = 3.000

    inception_batch_idx = 0
    log_reg_batch_idx = 1
    ksvm_batch_idx = 2
    resnet_batch_idx = 3

    for inception_reps, log_reg_reps, ksvm_reps, resnet_reps in five_hundred_ms_reps:
        total_cpus = range(9,29)

        def get_cpus(num_cpus):
            return [total_cpus.pop() for _ in range(num_cpus)]

        total_gpus = range(8)

        def get_gpus(num_gpus):
            return [total_gpus.pop() for _ in range(num_gpus)]

        configs = [
            setup_inception(batch_size=five_hundred_ms_batches[inception_batch_idx],
                            num_replicas=alexnet_reps,
                            cpus_per_replica=1,
                            allocated_cpus=get_cpus(inception_reps),
                            allocated_gpus=get_gpus(inception_reps)),
            setup_log_reg(batch_size=five_hundred_ms_batches[log_reg_batch_idx],
                          num_replicas=log_reg_reps,
                          cpus_per_replica=1,
                          allocated_cpus=get_cpus(log_reg_reps),
                          allocated_gpus=get_gpus(log_reg_reps)),
            setup_kernel_svm(batch_size=five_hundred_ms_batches[ksvm_batch_idx],
                             num_replicas=ksvm_reps,
                             cpus_per_replica=1,
                             allocated_cpus=get_cpus(ksvm_reps),
                             allocated_gpus=get_gpus(ksvm_reps)),
            setup_resnet(batch_size=five_hundred_ms_batches[resnet_batch_idx],
                         num_replicas=resnet_reps,
                         cpus_per_replica=1,
                         allocated_cpus=get_cpus(resnet_reps),
                         allocated_gpus=get_gpus(resnet_reps))
        ]

        client_num = 0

        benchmarker = DriverBenchmarker(model_configs, queue, client_num, five_hundred_ms_latency_upper_bound)

        p = Process(target=benchmarker.run)
        p.start()
        procs.append(p)

        all_stats = []
        all_stats.append(queue.get())

        cl = ClipperConnection(DockerContainerManager(redis_port=6380))
        cl.connect()

        fname = "incep_{}-logreg_{}-ksvm_{}-resnet_{}".format(inception_reps, log_reg_reps, ksvm_reps, resnet_reps)
        driver_utils.save_results(configs, cl, all_stats, "e2e_500ms_slo_img_driver_1", prefix=fname)
    
    sys.exit(0)