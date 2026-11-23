export type Project={id:string;name:string;service_identifier:string;retention_days?:number;sample_rate?:number};
export type Overview={requests:number;errors:number;error_rate:number;input_tokens:number|null;output_tokens:number|null;estimated_cost:string|null;average_latency_ms:number|null};
export type Trace={trace_id:string;root_operation:string;service_name:string;started_at:string;ended_at:string;duration_ms:number;status:string};
export type Span={span_id:string;parent_span_id:string|null;operation_name:string;span_kind:string;started_at:string;ended_at:string;duration_ms:number;status:string;attributes:Record<string,unknown>;model:string|null;provider:string|null;input_tokens:number|null;output_tokens:number|null;estimated_cost:string|null};
export type TraceDetail=Trace&{spans:Span[];critical_path_ms:number};
export const base=process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export async function get<T>(path:string):Promise<T>{const response=await fetch(`${base}${path}`,{cache:"no-store"});if(!response.ok)throw new Error(`API returned ${response.status}`);return response.json() as Promise<T>}
export function chooseProject(projects:Project[],requested?:string):Project|undefined{return projects.find(p=>p.id===requested)??projects[0]}
