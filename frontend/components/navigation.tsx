"use client";
import Link from "next/link";
import {usePathname,useRouter,useSearchParams} from "next/navigation";
import {useEffect,useState} from "react";
import {base,Project} from "@/lib/api";

export function Navigation(){const [projects,setProjects]=useState<Project[]>([]);const search=useSearchParams();const router=useRouter();const pathname=usePathname();const selected=search.get("project")??projects[0]?.id??"";useEffect(()=>{fetch(`${base}/api/projects`).then(r=>r.ok?r.json():[]).then(setProjects).catch(()=>setProjects([]))},[]);const href=(path:string)=>selected?`${path}?project=${selected}`:path;return <><nav><Link href={href("/")}>Overview</Link><Link href={href("/traces")}>Traces</Link><Link href={href("/costs")}>Costs</Link><Link href={href("/diagnostics")}>Diagnostics</Link><Link href={href("/projects")}>Projects</Link></nav><select aria-label="Active project" value={selected} onChange={event=>router.push(`${pathname}?project=${event.target.value}`)}><option value="">Select project</option>{projects.map(project=><option key={project.id} value={project.id}>{project.name}</option>)}</select></>}
