package ai.clipper.serve

import ai.clipper.Clipper
import ai.clipper.container.Container
import org.apache.spark.mllib.util.MLUtils
import org.apache.spark.{SparkConf, SparkContext}

object Serve {

  def main(args: Array[String]): Unit = {

    val modelPath = sys.env("CLIPPER_MODEL_DATA")
    // val modelName = sys.env("CLIPPER_MODEL_NAME")
    // val modelVersion = sys.env("CLIPPER_MODEL_VERSION").toInt

    val conf = new SparkConf()
      .setAppName("ClipperMLLibContainer")
      .setMaster("local")
    val sc = new SparkContext(conf)
    sc.parallelize(Seq(""))
      .foreachPartition(x => {
        import org.apache.commons.logging.LogFactory
        import org.apache.log4j.{Level, LogManager}
        LogManager.getRootLogger().setLevel(Level.WARN)
        val log = LogFactory.getLog("EXECUTOR-LOG:")
        log.warn("START EXECUTOR WARN LOG LEVEL")
      })

    // Load and parse the data file.
    val data = MLUtils.loadLibSVMFile(
      sc,
      "/Users/crankshaw/model-serving/spark_serialization_project/spark_binary/data/mllib/sample_libsvm_data.txt")
    // Split the data into training and test sets (30% held out for testing)
    val splits = data.randomSplit(Array(0.7, 0.3))
    val (trainingData, testData) = (splits(0), splits(1))
    val container: Container =
      Clipper.loadModel(sc, modelPath)

    val labelAndPreds = data.collect().map { point =>
      val prediction = container.predict(point.features)
      (point.label, prediction)
    }
    val numWrong = labelAndPreds.filter(r => r._1 != r._2).size
    println(s"Test Error $numWrong")
  }

}
