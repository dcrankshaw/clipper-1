#include <cstdlib>
#include <fstream>
#include <iostream>
#include <time.h>
#include <cmath>
#include <random>

// #include "httplib.h"
// #define HTTP_IMPLEMENTATION
// #include "http.h"
#include "predictor.hpp"
#include "zmq_client.hpp"

namespace zmq_client {

using namespace clipper;

constexpr int SEND_PORT = 4456;
constexpr int RECV_PORT = 4455;

Driver::Driver(std::function<void(FrontendRPCClient &, ClientFeatureVector,
                                  std::atomic<int> &)>
                   predict_func,
               std::vector<ClientFeatureVector> inputs, float target_throughput,
               std::string distribution, int trial_length, int num_trials,
               std::string log_file, std::string clipper_address)
    : predict_func_(predict_func),
      inputs_(inputs),
      target_throughput_(target_throughput),
      distribution_(distribution),
      trial_length_(trial_length),
      num_trials_(num_trials),
      log_file_(log_file),
      client_{2},
      done_(false),
      prediction_counter_(0),
      clipper_address_(clipper_address) {
  client_.start(clipper_address, SEND_PORT, RECV_PORT);
}

void spin_sleep(long duration_micros) {
  auto start_time = std::chrono::system_clock::now();
  long cur_delay_micros = 0;
  while (cur_delay_micros < duration_micros) {
    auto cur_delay = std::chrono::system_clock::now() - start_time;
    cur_delay_micros =
        std::chrono::duration_cast<std::chrono::microseconds>(cur_delay).count();
  }
}

void Driver::start() {
  if (!(distribution_ == "poisson" || distribution_ == "constant")) {
    std::cerr << "Invalid distribution: " << distribution_ << std::endl;
    return;
  }

  std::random_device rd;
  std::mt19937 gen(rd());
  std::exponential_distribution<> exp_dist(target_throughput_);
  long constant_request_delay_micros = std::lround(1.0 / target_throughput_ * 1000.0 * 1000.0);

  auto monitor_thread = std::thread([this]() { monitor_results(); });
  while (!done_) {
    for (ClientFeatureVector f : inputs_) {
      if (done_) {
        break;
      }
      predict_func_(client_, f, prediction_counter_);
      
      if (distribution_ == "poisson") {
        float delay_secs = exp_dist(gen);
        long delay_micros = lround(delay_secs * 1000.0 * 1000.0);
        spin_sleep(delay_micros);
      } else if (distribution_ == "constant") {
        spin_sleep(constant_request_delay_micros);
        // struct timespec req = {0};
        // req.tv_sec = 0;
        // req.tv_nsec = request_delay_micros_ * 1000;
        // nanosleep(&req, (struct timespec *)NULL);
        // std::this_thread::sleep_for(
        //     std::chrono::microseconds(request_delay_micros_));
      }
    }
  }


  client_.stop();
  monitor_thread.join();
}

void Driver::monitor_results() {
  int num_completed_trials = 0;
  std::ofstream client_metrics_file;
  std::ofstream clipper_metrics_file;
  client_metrics_file.open(log_file_ + "-client_metrics.json");
  clipper_metrics_file.open(log_file_ + "-clipper_metrics.json");
  client_metrics_file << "[" << std::endl;
  clipper_metrics_file << "[" << std::endl;
  // httplib::Client http_client(clipper_address_.c_str(), 1337);

  metrics::MetricsRegistry &registry = metrics::MetricsRegistry::get_metrics();

  while (!done_) {
    int current_count = prediction_counter_;
    if (current_count > trial_length_) {
      prediction_counter_ = 0;
      num_completed_trials += 1;
      std::cout << "Trial " << std::to_string(num_completed_trials)
                << " completed" << std::endl;
      std::string metrics_report =
          // registry.report_metrics(false);
          registry.report_metrics(true);
      client_metrics_file << metrics_report;
      client_metrics_file << "," << std::endl;
      std::string address = "http://" + clipper_address_ + ":" +
                            std::to_string(1337) + "/metrics";
      std::string cmd_str = "curl -s -S " + address + " > curl_out.txt";
      std::system(cmd_str.c_str());
      std::ifstream curl_output("curl_out.txt");
      std::stringstream curl_str_buf;
      curl_output >> curl_str_buf.rdbuf();
      std::string curl_str = curl_str_buf.str();
      clipper_metrics_file << curl_str;
      clipper_metrics_file << "," << std::endl;
      curl_output.close();
    }

    if (num_completed_trials >= num_trials_) {
      done_ = true;
    } else {
      std::this_thread::sleep_for(std::chrono::seconds(1));
    }
  }

  // client_metrics_file << "]";
  client_metrics_file.close();
  // clipper_metrics_file << "]";
  clipper_metrics_file.close();
  return;
}
}
