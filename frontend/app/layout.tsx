import "./globals.css";
import {Suspense} from "react";
import type {Metadata} from "next";
import {Navigation} from "@/components/navigation";

export const metadata:Metadata={title:{default:"TAMGA AI",template:"%s · TAMGA AI"},description:"AI agent observability for latency, failures, tokens, and cost."};

export default function Layout({children}:{children:React.ReactNode}){return <html lang="en"><body><header><strong>TAMGA <span>AI</span></strong><Suspense fallback={<nav>Loading navigation…</nav>}><Navigation/></Suspense></header>{children}</body></html>}
