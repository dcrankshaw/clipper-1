from __future__ import absolute_import, print_function
import os
import sys
import requests
import json
import numpy as np
cur_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath("%s/.." % cur_dir))
from clipper_admin import Clipper
import time
import subprocess32 as subprocess
import pprint
import random
import socket


import findspark
findspark.init()
import pyspark
from pyspark import SparkConf, SparkContext
from pyspark.mllib.classification import LogisticRegressionWithSGD
from pyspark.mllib.classification import SVMWithSGD
from pyspark.mllib.tree import RandomForestModel
from pyspark.mllib.regression import LabeledPoint


headers = {'Content-type': 'application/json'}
app_name = "pyspark_test"
model_name = "pyspark_model"


class BenchmarkException(Exception):
    def __init__(self, value):
        self.parameter = value

    def __str__(self):
        return repr(self.parameter)


# range of ports where available ports can be found
PORT_RANGE = [34256, 40000]


def find_unbound_port():
    """
    Returns an unbound port number on 127.0.0.1.
    """
    while True:
        port = random.randint(*PORT_RANGE)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return port
        except socket.error:
            print("randomly generated port %d is bound. Trying again." % port)


def init_clipper():
    clipper = Clipper("localhost", redis_port=find_unbound_port())
    clipper.stop_all()
    clipper.start()
    time.sleep(1)
    return clipper


def normalize(x):
    return x.astype(np.double) / 255.0


def objective(y, pos_label):
    # prediction objective
    if y == pos_label:
        return 1
    else:
        return 0

def parseData(line, obj, pos_label):
    fields = line.strip().split(',')
    # return LabeledPoint(obj(int(fields[0]), pos_label), [float(v)/255.0 for v in fields[1:]])
    return LabeledPoint(obj(int(fields[0]), pos_label), normalize(np.array(fields[1:])))

def predict(sc, model, xs):
    return [str(model.predict(normalize(x))) for x in xs]


def deploy_and_test_model(sc, clipper, model, version, test_data):
    clipper.deploy_pyspark_model(model_name, version, predict, model, sc, ["a"], "ints")
    time.sleep(10)
    num_preds = 25
    num_defaults = 0
    for i in range(num_preds):
        response = requests.post(
            "http://localhost:1337/%s/predict" % app_name,
            headers=headers,
            data=json.dumps({'input': list(test_data[np.random.randint(len(test_data))])}))
        result = response.json()
        if response.status_code == requests.codes.ok and result["default"] == True:
            num_defaults += 1
    if num_defaults > 0:
        print("Error: %d/%d predictions were default" % (num_defaults,
                                                         num_preds))
    if num_defaults > num_preds / 2:
        raise BenchmarkException("Error querying APP %s, MODEL %s:%d" %
                                 (app_name, model_name, version))

def train_logistic_regression(trainRDD):
    return LogisticRegressionWithSGD.train(trainRDD, iterations=10)

def train_svm(trainRDD):
    return SVMWithSGD.train(trainRDD)


def train_random_forest(trainRDD, num_trees, max_depth):
    return RandomForest.trainClassifier(trainRDD, 2, {}, num_trees, maxDepth=depth)

if __name__ == "__main__":
    pos_label = 3
    conf = SparkConf() \
        .setAppName("crankshaw-pyspark") \
        .set("spark.executor.memory", "2g") \
        .set("master", "local")
    try:
        sc = SparkContext(conf=conf, batchSize=10)
        clipper = init_clipper()

        train_path = "/Users/crankshaw/code/amplab/model-serving/data/mnist_data/train.data"
        trainRDD = sc.textFile(train_path).map(lambda line: parseData(line, objective, pos_label)).cache()

        test_path = "/Users/crankshaw/code/amplab/model-serving/data/mnist_data/test.data"
        with open(test_path, "r") as test_file:
            test_data = [np.array(l.strip().split(",")[1:]).astype(np.int) for l in test_file]

        try:
            clipper.register_application(app_name, model_name, "ints", "default_pred", 100000)
            time.sleep(1)
            response = requests.post(
                "http://localhost:1337/%s/predict" % app_name,
                headers=headers,
                data=json.dumps({'input': list(test_data[np.random.randint(len(test_data))])}))
            result = response.json()
            if response.status_code != requests.codes.ok:
                print("Error: %s" % response.text)
                raise BenchmarkException("Error creating app %s" % app_name)

            version = 1
            lr_model = train_logistic_regression(trainRDD)
            deploy_and_test_model(sc, clipper, lr_model, version, test_data)

            version += 1
            svm_model = train_svm(trainRDD)
            deploy_and_test_model(sc, clipper, svm_model, version, test_data)

            version += 1
            rf_model = train_random_forest(trainRDD, 20, 16)
            deploy_and_test_model(sc, clipper, svm_model, version, test_data)
        except BenchmarkException as e:
            print(e)
            clipper.stop_all()
            sc.stop()
            sys.exit(1)
        else:
            sc.stop()
            clipper.stop_all()
    except:
        clipper = Clipper("localhost")
        clipper.stop_all()
        sys.exit(1)

