import Link from "next/link";
import {chooseProject,get,Project,Trace} from "@/lib/api";

type Query={project?:string;search?:string;status?:string;service?:string;agent?:string;model?:string;min_duration_ms?:string;offset?:string};
export default async function Traces({searchParams}:{searchParams:Promise<Query>}) {
  const query=await searchParams;
  const projects=await get<Project[]>("/api/projects");
  const project=chooseProject(projects,query.project);
  if(!project)return <main><h1>Traces</h1><div className="panel empty">No project selected.</div></main>;
  const params=new URLSearchParams({limit:"50",offset:query.offset??"0"});
  for(const key of ["search","status","service","agent","model","min_duration_ms"] as const) if(query[key]) params.set(key,query[key]!);
  const data=await get<{items:Trace[];total:number;limit:number;offset:number}>(`/api/projects/${project.id}/traces?${params}`);
  const pageParams=(offset:number)=>{const value=new URLSearchParams();Object.entries(query).forEach(([key,item])=>{if(item)value.set(key,item)});value.set("project",project.id);value.set("offset",String(offset));return `/traces?${value}`};
  return <main>
    <h1>Traces</h1><p className="lead">{data.total} persisted OTLP executions</p>
    <form className="panel filter-form">
      <input type="hidden" name="project" value={project.id}/>
      <label>Search<input name="search" defaultValue={query.search} placeholder="Trace ID or operation"/></label>
      <label>Status<select name="status" defaultValue={query.status??""}><option value="">All</option><option value="OK">OK</option><option value="ERROR">Error</option></select></label>
      <label>Service<input name="service" defaultValue={query.service} placeholder="demo-agent"/></label>
      <label>Agent<input name="agent" defaultValue={query.agent} placeholder="research"/></label>
      <label>Model<input name="model" defaultValue={query.model} placeholder="simulated-model"/></label>
      <label>Minimum ms<input name="min_duration_ms" type="number" min="0" defaultValue={query.min_duration_ms}/></label>
      <button>Apply filters</button><Link className="trace-link" href={`/traces?project=${project.id}`}>Clear</Link>
    </form>
    <section className="panel">{data.items.length?<table><thead><tr><th>Operation</th><th>Service</th><th>Duration</th><th>Status</th></tr></thead><tbody>{data.items.map(t=><tr key={t.trace_id}><td><Link className="trace-link" href={`/traces/${t.trace_id}?project=${project.id}`}>{t.root_operation}</Link><br/><small>{t.trace_id}</small></td><td>{t.service_name}</td><td>{t.duration_ms.toFixed(1)} ms</td><td><span className={`status ${t.status.toLowerCase()}`}>{t.status}</span></td></tr>)}</tbody></table>:<div className="empty">No traces match these filters.</div>}</section>
    <div className="pagination">{data.offset>0&&<Link href={pageParams(Math.max(0,data.offset-data.limit))}>← Previous</Link>}<span>{data.offset+1}–{Math.min(data.offset+data.limit,data.total)} of {data.total}</span>{data.offset+data.limit<data.total&&<Link href={pageParams(data.offset+data.limit)}>Next →</Link>}</div>
  </main>;
}
