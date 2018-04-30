#include <atomic>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iostream>
#include <random>
#include <string>

#include <cxxopts.hpp>

#include <clipper/clock.hpp>
#include <clipper/metrics.hpp>

#include "driver.hpp"
#include "inputs.hpp"
#include "zmq_client.hpp"

using namespace clipper;
using namespace zmq_client;

const std::string DEFAULT_WORKLOAD_PATH = "default_path";

class ProfilerMetrics {
 public:
  explicit ProfilerMetrics(std::string name)
      : name_(name),
        latency_(clipper::metrics::MetricsRegistry::get_metrics().create_histogram(
            name_ + ":prediction_latency", "microseconds", 32768)),
        latency_list_(clipper::metrics::MetricsRegistry::get_metrics().create_data_list<long long>(
            name_ + ":prediction_latencies", "microseconds")),
        throughput_(clipper::metrics::MetricsRegistry::get_metrics().create_meter(
            name_ + ":prediction_throughput")),
        num_predictions_(clipper::metrics::MetricsRegistry::get_metrics().create_counter(
            name_ + ":num_predictions")) {}

  ~ProfilerMetrics() = default;

  ProfilerMetrics(const ProfilerMetrics&) = default;

  ProfilerMetrics& operator=(const ProfilerMetrics&) = default;

  ProfilerMetrics(ProfilerMetrics&&) = default;
  ProfilerMetrics& operator=(ProfilerMetrics&&) = default;

  std::string name_;
  std::shared_ptr<clipper::metrics::Histogram> latency_;
  std::shared_ptr<clipper::metrics::DataList<long long>> latency_list_;
  std::shared_ptr<clipper::metrics::Meter> throughput_;
  std::shared_ptr<clipper::metrics::Counter> num_predictions_;
};

void predict(std::shared_ptr<FrontendRPCClient> client, std::string name, ClientFeatureVector input,
             ProfilerMetrics metrics, std::atomic<int>& prediction_counter,
             std::ofstream& query_lineage_file, std::mutex& query_file_mutex) {
  auto start_time = std::chrono::system_clock::now();
  client->send_request(name, input, [metrics, &prediction_counter, start_time, &query_lineage_file,
                                    &query_file_mutex](ClientFeatureVector output,
                                                       std::shared_ptr<QueryLineage> lineage) {
    if (output.type_ == DataType::Strings) {
      std::string output_str =
          std::string(reinterpret_cast<char*>(output.get_data()), output.size_typed_);
      if (output_str == "TIMEOUT") {
        return;
      }
    }
    auto cur_time = std::chrono::system_clock::now();
    auto latency = cur_time - start_time;
    long latency_micros = std::chrono::duration_cast<std::chrono::microseconds>(latency).count();
    metrics.latency_->insert(static_cast<int64_t>(latency_micros));
    metrics.latency_list_->insert(static_cast<int64_t>(latency_micros));
    metrics.throughput_->mark(1);
    metrics.num_predictions_->increment(1);
    prediction_counter += 1;
    lineage->add_timestamp("driver::send", std::chrono::duration_cast<std::chrono::microseconds>(
                                               start_time.time_since_epoch())
                                               .count());

    lineage->add_timestamp(
        "driver::recv",
        std::chrono::duration_cast<std::chrono::microseconds>(cur_time.time_since_epoch()).count());

    std::unique_lock<std::mutex> lock(query_file_mutex);
    query_lineage_file << "{";
    int num_entries = lineage->get_timestamps().size();
    int idx = 0;
    for (auto& entry : lineage->get_timestamps()) {
      query_lineage_file << "\"" << entry.first << "\": " << std::to_string(entry.second);
      if (idx < num_entries - 1) {
        query_lineage_file << ", ";
      }
      idx += 1;
    }
    query_lineage_file << "}" << std::endl;
  });
}

int main(int argc, char* argv[]) {

  cxxopts::Options options("profiler", "InferLine profiler");
  // clang-format off
  options.add_options()
      ("name", "Model name",
       cxxopts::value<std::string>())
      ("input_type", "Only \"float\" supported for now.",
       cxxopts::value<std::string>()->default_value("float"))
      ("input_size", "length of each input",
       cxxopts::value<int>())
      // ("request_delay_micros", "Request delay in integer microseconds",
      //  cxxopts::value<int>())
      ("target_throughput", "Mean throughput to target in qps",
       cxxopts::value<float>())
      ("request_distribution", "Distribution to sample request delay from. "
       "Can be 'constant', 'poisson', or 'batch'. 'batch' sends a single batch at a time.",
       cxxopts::value<std::string>())
      ("trial_length", "Number of queries per trial",
       cxxopts::value<int>())
      ("num_trials", "Number of trials",
       cxxopts::value<int>())
      ("batch_size", "Batch size",
       cxxopts::value<int>()->default_value("-1"))
      ("log_file", "location of log file",
       cxxopts::value<std::string>())
      ("clipper_address", "IP address or hostname of ZMQ frontend",
       cxxopts::value<std::string>())
      ("workload_path", "(Optional) path to the input workload",
       cxxopts::value<std::string>()->default_value(DEFAULT_WORKLOAD_PATH))
       ;
  // clang-format on
  options.parse(argc, argv);
  std::string distribution = options["request_distribution"].as<std::string>();
  if (!(distribution == "poisson" || distribution == "constant" || distribution == "batch")) {
    std::cerr << "Invalid distribution: " << distribution << std::endl;
    return 1;
  }

  // Request the system uptime so that a clock instance is created as
  // soon as the frontend starts
  clock::ClipperClock::get_clock().get_uptime();

  std::string model_name = options["name"].as<std::string>();
  size_t input_size = static_cast<size_t>(options["input_size"].as<int>());

  std::string opts_workload_path = options["workload_path"].as<std::string>();
  boost::optional<std::string> workload_path;
  if (opts_workload_path != DEFAULT_WORKLOAD_PATH) {
    workload_path = boost::optional<std::string>(opts_workload_path);
  }

  std::vector<ClientFeatureVector> inputs = generate_inputs(model_name, input_size, workload_path);
  ProfilerMetrics metrics{model_name};

  std::ofstream query_lineage_file;
  std::mutex query_file_mutex;
  query_lineage_file.open(options["log_file"].as<std::string>() + "-query_lineage.txt");

  auto predict_func = [metrics, model_name, &query_lineage_file, &query_file_mutex](
      std::unordered_map<std::string, std::shared_ptr<FrontendRPCClient>> clients, ClientFeatureVector input,
      std::atomic<int>& prediction_counter) {
    predict(clients[model_name], model_name, input, metrics, prediction_counter,
        query_lineage_file, query_file_mutex);
  };
  std::unordered_map<std::string, std::string> addresses = {
    {model_name, options["clipper_address"].as<std::string>()}
  };
  Driver driver(predict_func, std::move(inputs), options["target_throughput"].as<float>(),
                distribution, options["trial_length"].as<int>(), options["num_trials"].as<int>(),
                options["log_file"].as<std::string>(), addresses,
                options["batch_size"].as<int>(), {}, true);
  std::cout << "Starting driver" << std::endl;
  driver.start();
  std::cout << "Driver completed" << std::endl;
  query_lineage_file.close();
  return 0;
}