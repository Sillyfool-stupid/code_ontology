import io.shiftleft.semanticcpg.language.*
import io.shiftleft.codepropertygraph.generated.nodes.StoredNode
import java.nio.file.{Files, Paths}
import java.nio.charset.StandardCharsets

// Streaming export for Joern 4 / flatgraph. IDs are strings to preserve 64-bit precision.
@main def exec(cpgFile: String, output: String): Unit = {
  importCpg(cpgFile)
  val target = Paths.get(output)
  Files.createDirectories(target)
  def value(v: Any): ujson.Value = v match {
    case null => ujson.Null
    case x: String => ujson.Str(x)
    case x: Boolean => ujson.Bool(x)
    case x: Int => ujson.Num(x)
    case x: Long => ujson.Str(x.toString)
    case x: Double => ujson.Num(x)
    case x: Float => ujson.Num(x.toDouble)
    case x: Option[?] => x.map(value).getOrElse(ujson.Null)
    case x: Iterable[?] => ujson.Arr.from(x.iterator.map(value))
    case x: Array[?] => ujson.Arr.from(x.iterator.map(value))
    case x => ujson.Str(x.toString)
  }
  val nodes = Files.newBufferedWriter(target.resolve("nodes.jsonl"), StandardCharsets.UTF_8)
  try {
    cpg.graph.allNodes.foreach { node =>
      val properties = ujson.Obj.from(node.asInstanceOf[StoredNode].properties.iterator.map { case (k,v) => (k, value(v)) })
      nodes.write(ujson.write(ujson.Obj("id" -> node.id.toString, "label" -> node.label, "properties" -> properties)))
      nodes.newLine()
    }
  } finally nodes.close()
  val edges = Files.newBufferedWriter(target.resolve("edges.jsonl"), StandardCharsets.UTF_8)
  try {
    cpg.graph.allEdges.foreach { edge =>
      edges.write(ujson.write(ujson.Obj("src" -> edge.src.id.toString, "dst" -> edge.dst.id.toString, "label" -> edge.label)))
      edges.newLine()
    }
  } finally edges.close()
  val meta = ujson.Obj("joern" -> "flatgraph", "nodes" -> cpg.graph.nodeCount, "edges" -> cpg.graph.edgeCount)
  Files.writeString(target.resolve("export.json"), ujson.write(meta, indent=2))
  close
}
