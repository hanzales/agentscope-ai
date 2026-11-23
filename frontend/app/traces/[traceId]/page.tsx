import Link from "next/link";
import {chooseProject,get,Project,TraceDetail} from "@/lib/api";

export default async function TracePage({
  params,
  searchParams,
}: {
  params: Promise<{traceId:string}>;
  searchParams: Promise<{project?:string}>;
}) {
  const {traceId}=await params;
  const query=await searchParams;
  const projects=await get<Project[]>("/api/projects");
  const project=chooseProject(projects,query.project);
  if(!project) return <main><h1>Trace</h1><div className="panel empty">Project not found.</div></main>;
  const trace=await get<TraceDetail>(`/api/projects/${project.id}/traces/${traceId}`);
  const origin=Math.min(...trace.spans.map(span=>new Date(span.started_at).getTime()));
  const total=Math.max(trace.duration_ms,1);
  return <main>
    <Link className="trace-link" href={`/traces?project=${project.id}`}>← Back to traces</Link>
    <div className="page-title">
      <div><h1>{trace.root_operation}</h1><p className="lead">{trace.trace_id} · {trace.service_name}</p></div>
      <span className={`status ${trace.status.toLowerCase()}`}>{trace.status}</span>
    </div>
    <section className="grid">
      <Metric label="Duration" value={`${trace.duration_ms.toFixed(1)} ms`}/>
      <Metric label="Critical path" value={`${trace.critical_path_ms.toFixed(1)} ms`}/>
      <Metric label="Spans" value={trace.spans.length}/>
      <Metric label="Model tokens" value={trace.spans.reduce((sum,span)=>sum+(span.input_tokens??0)+(span.output_tokens??0),0)}/>
    </section>
    <section className="panel">
      <h2>Execution waterfall</h2>
      <div className="waterfall-head"><span>Operation</span><span>Timeline</span><span>Duration</span></div>
      {trace.spans.map(span=>{
        const left=(new Date(span.started_at).getTime()-origin)/total*100;
        const width=Math.max(span.duration_ms/total*100,1);
        return <div className="waterfall-row" key={span.span_id}>
          <div className="operation"><strong>{span.operation_name}</strong><small>{String(span.attributes["agentscope.span.type"]??span.span_kind).toLowerCase()} · {span.status}</small></div>
          <div className="track"><span className={`bar ${span.status.toLowerCase()}`} style={{left:`${Math.min(left,99)}%`,width:`${Math.min(width,100-left)}%`}}/></div>
          <div>{span.duration_ms.toFixed(1)} ms</div>
        </div>;
      })}
    </section>
    <section className="panel">
      <h2>Span details</h2>
      {trace.spans.map(span=><details key={span.span_id}>
        <summary>{span.operation_name} <small>{span.span_id}</small></summary>
        <dl>
          <dt>Parent</dt><dd>{span.parent_span_id??"Root"}</dd>
          <dt>Model</dt><dd>{span.provider&&span.model?`${span.provider} / ${span.model}`:"—"}</dd>
          <dt>Tokens</dt><dd>{span.input_tokens===null&&span.output_tokens===null?"Unknown":`${span.input_tokens??0} in / ${span.output_tokens??0} out`}</dd>
          <dt>Attributes</dt><dd><pre>{JSON.stringify(span.attributes,null,2)}</pre></dd>
        </dl>
      </details>)}
    </section>
  </main>;
}

function Metric({label,value}:{label:string;value:string|number}) {
  return <article className="card"><div className="label">{label}</div><div className="value">{value}</div></article>;
}
