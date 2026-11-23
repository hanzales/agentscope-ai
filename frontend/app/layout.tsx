import "./globals.css";
import Link from "next/link";
export default function Layout({children}:{children:React.ReactNode}){return <html lang="en"><body><header><strong>AgentScope <span>AI</span></strong><nav><Link href="/">Overview</Link><Link href="/traces">Traces</Link><Link href="/costs">Costs</Link><Link href="/diagnostics">Diagnostics</Link></nav></header>{children}</body></html>}
