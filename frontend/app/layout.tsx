import "./globals.css";
import {Suspense} from "react";
import {Navigation} from "@/components/navigation";
export default function Layout({children}:{children:React.ReactNode}){return <html lang="en"><body><header><strong>AgentScope <span>AI</span></strong><Suspense fallback={<nav>Loading navigation…</nav>}><Navigation/></Suspense></header>{children}</body></html>}
