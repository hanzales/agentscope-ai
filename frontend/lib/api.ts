export type Project={id:string;name:string;service_identifier:string};
export type Overview={requests:number;errors:number;error_rate:number;input_tokens:number|null;output_tokens:number|null;estimated_cost:string|null;average_latency_ms:number|null};
const base=process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export async function get<T>(path:string):Promise<T>{const response=await fetch(`${base}${path}`,{cache:"no-store"});if(!response.ok)throw new Error(`API returned ${response.status}`);return response.json() as Promise<T>}
