package ai.clipper.examples.train

import org.apache.spark.mllib.classification.LogisticRegressionWithLBFGS
import org.apache.spark.mllib.linalg.Vector
import org.apache.spark.mllib.util.MLUtils
import org.apache.spark.{SparkConf, SparkContext}
import ai.clipper.spark.{
  Clipper,
  MLlibContainer,
  MLlibLogisticRegressionModel,
  MLlibModel
}

class LogisticRegressionContainer extends MLlibContainer {

  var model: Option[MLlibModel] = None

  override def init(sc: SparkContext, m: MLlibModel) {
    println("Initializing container")
    model = Some(m)
  }

  override def predict(xs: List[Vector]): List[Float] = {
    println("making prediction")
    val m = model.get
    xs.map(x => m.predict(x).toFloat)
  }
}

object Train {

  def main(args: Array[String]): Unit = {
    val conf = new SparkConf().setAppName("ClipperTest").setMaster("local[2]")
    val sc = new SparkContext(conf)
    sc.parallelize(Seq(""))
      .foreachPartition(x => {
        import org.apache.commons.logging.LogFactory
        import org.apache.log4j.{Level, LogManager}
        LogManager.getRootLogger().setLevel(Level.WARN)
        val log = LogFactory.getLog("EXECUTOR-LOG:")
        log.warn("START EXECUTOR WARN LOG LEVEL")
      })

    val sparkHome = sys.env.get("SPARK_HOME").get
    // Load and parse the data file.
    val data = MLUtils.loadLibSVMFile(
      sc,
      s"${sparkHome}/data/mllib/sample_libsvm_data.txt")
    // Split the data into training and test sets (30% held out for testing)
    val splits = data.randomSplit(Array(0.7, 0.3))
    val (trainingData, testData) = (splits(0), splits(1))

    val numClasses = 2

    val model = MLlibLogisticRegressionModel(
      new LogisticRegressionWithLBFGS()
        .setNumClasses(numClasses)
        .run(trainingData))

    val clipperHost = sys.env.getOrElse("CLIPPER_HOST", "localhost")
    val clipperVersion = sys.env.getOrElse("CLIPPER_MODEL_VERSION", "1").toInt
    val sshKeyPath = sys.env.get("SSH_KEY_PATH")
    val sshUser = sys.env.get("SSH_USER")

    // Evaluate model on test instances and compute test error
    val labelAndPreds = testData.map { point =>
      val prediction = model.predict(point.features)
      (point.label, prediction)
    }
    val numWrong = labelAndPreds.filter(r => r._1 != r._2).count()
    println(s"Num wrong: $numWrong")
    println("Learned logistic regression model:\n" + model.toString)

    Clipper.deploySparkModel(sc,
                             sys.env("CLIPPER_MODEL_NAME"),
                             clipperVersion,
                             model,
                             classOf[LogisticRegressionContainer],
                             clipperHost,
                             List("a"),
                             sshUser,
                             sshKeyPath)
    sc.stop()
  }
}
