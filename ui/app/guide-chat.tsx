"use client";

import { BookOpenText, ChevronRight, LoaderCircle, Search, Send, Square, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { apiRequest } from "@/lib/api-client";

type Reference = { path: string; heading: string; anchor: string; part: number; sha256: string };
type Source = { id: string; text: string; references: Reference[]; number: number; score: number };
type Paragraph = { text: string; support: { source: number; quote: string }[] };
type Reply = { status: "answered" | "insufficient_evidence"; paragraphs: Paragraph[]; message: string; sources: Source[]; corpus_sha256: string; model: string };
type Turn = { id: number; question: string; reply?: Reply; error?: string; searchOnly?: boolean };
type GuideStatus = { ready: boolean; model: string; retrieval: string; documents: number; chunks: number; corpus_sha256: string; message: string; sources: { path: string; sha256: string }[] };
const suggested = ["How do Red, Blue and the controlled agent work together?", "What survives a reset?", "What is implemented, and what still needs real-model acceptance?", "How do datasets, checkpoints and paired evaluations connect?"];

export function CitedParagraph({ paragraph, sources, onSource }: { paragraph: Paragraph; sources: Source[]; onSource: (source: Source) => void }) {
  return <div className="guide-paragraph"><p>{paragraph.text}</p><div className="guide-citations">{[...new Set(paragraph.support.map(item => item.source))].map(number => {
    const source = sources.find(item => item.number === number);
    return source ? <button key={number} onClick={() => onSource(source)} aria-label={`Inspect source ${number}: ${source.references[0].heading}`}>[{number}] {source.references[0].heading.split(" / ").at(-1)}</button> : null;
  })}</div><details className="guide-quotes"><summary>Supporting quotes</summary>{paragraph.support.map((item, index) => <blockquote key={index}>[{item.source}] {item.quote}</blockquote>)}</details></div>;
}

export default function GuideChat({ apiBase }: { apiBase: string }) {
  const [status, setStatus] = useState<GuideStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [statusRevision, setStatusRevision] = useState(0);
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<Source | null>(null);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const nextId = useRef(1);
  const latest = useRef<HTMLDivElement | null>(null);
  const composer = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    const abort = new AbortController();
    apiRequest<GuideStatus>(apiBase, "/v1/guide/status", { signal: abort.signal })
      .then(result => { if (!abort.signal.aborted) { setStatus(result); setStatusError(""); } })
      .catch(error => { if (!abort.signal.aborted) setStatusError(error instanceof Error ? error.message : "The documentation service is unavailable."); });
    return () => abort.abort();
  }, [apiBase, statusRevision]);
  useEffect(() => () => pending.current?.abort(), []);
  useEffect(() => { if (turns.length) latest.current?.scrollIntoView({ block: "nearest", behavior: "instant" }); }, [turns, busy]);

  async function ask(value: string, searchOnly = false) {
    const text = value.trim();
    if (text.length < 2 || text.length > 2000 || pending.current || !status) return;
    const id = nextId.current++;
    const abort = new AbortController();
    pending.current = abort;
    setBusy(true); setQuestion(""); setSelected(null);
    setTurns(previous => [...previous, { id, question: text, searchOnly }]);
    const history = turns.filter(turn => turn.reply?.status === "answered").slice(-3).flatMap(turn => [
      { role: "user", content: turn.question },
      { role: "assistant", content: turn.reply!.paragraphs.map(item => item.text).join("\n\n").slice(0, 6000) },
    ]);
    try {
      const options = { method: "POST", body: JSON.stringify({ question: text, history }), signal: abort.signal };
      let reply: Reply;
      if (searchOnly) {
        const result = await apiRequest<{ sources: Source[]; corpus_sha256: string }>(apiBase, "/v1/guide/search", options);
        reply = { ...result, status: "insufficient_evidence", paragraphs: [], message: result.sources.length ? "Matching source passages" : "No matching passages found. Try an AML component or workflow.", model: "" };
      } else reply = await apiRequest<Reply>(apiBase, "/v1/guide/chat", options);
      if (!abort.signal.aborted) setTurns(previous => previous.map(turn => turn.id === id ? { ...turn, reply } : turn));
    } catch (error) {
      setTurns(previous => previous.map(turn => turn.id === id ? { ...turn, error: abort.signal.aborted ? "Stopped. The provider may already have processed this request." : error instanceof Error ? error.message : "The answer could not be loaded." } : turn));
    } finally {
      pending.current = null; setBusy(false); composer.current?.focus();
    }
  }
  function submit(event: FormEvent) { event.preventDefault(); void ask(question, !status?.ready); }
  function showSource(source: Source) { setSelected(source); setSourcesOpen(true); }

  return <section className="guide-chat" aria-label="AML documentation assistant">
    <div className="guide-chat-heading">
      <div><span className="guide-eyebrow"><BookOpenText size={16} aria-hidden="true" /> YOUR PROJECT KNOWLEDGE</span><h2>Ask about AML.</h2><p>Answers from the architecture guide and its linked documents, with evidence you can inspect.</p></div>
      <Button variant="outline" disabled={busy || !turns.length} onClick={() => { setTurns([]); setSelected(null); composer.current?.focus(); }}><Trash2 aria-hidden="true" /> New conversation</Button>
    </div>
    {statusError ? <div className="guide-notice" role="alert"><p>{statusError}</p><Button variant="outline" onClick={() => setStatusRevision(value => value + 1)}>Retry connection</Button></div> : null}
    {status && !status.ready ? <div className="guide-notice" role="status"><strong>Search is ready. Answer generation needs a model key.</strong><p>{status.message}</p><Button variant="outline" size="sm" onClick={() => setStatusRevision(value => value + 1)}>Check configuration again</Button></div> : null}
    <div className="guide-chat-layout">
      <div className="surface guide-conversation">
        <div className="guide-conversation-bar"><span>{status ? `${status.documents} documents · ${status.ready ? status.model : "source search"}` : "Connecting to documentation…"}</span><button onClick={() => setSourcesOpen(value => !value)} aria-expanded={sourcesOpen} aria-controls="guide-sources">Sources <ChevronRight size={14} aria-hidden="true" /></button></div>
        <div className="guide-messages" aria-busy={busy}>
          {!turns.length ? <div className="guide-empty"><BookOpenText size={30} aria-hidden="true" /><h3>Start with a question.</h3><p>Explore components, lifecycle boundaries, evidence and the research workflow.</p><div className="guide-suggestions">{suggested.map(text => <button key={text} disabled={!status || busy} onClick={() => void ask(text, !status?.ready)}>{text}<ChevronRight size={16} aria-hidden="true" /></button>)}</div></div> : null}
          {turns.map(turn => <article className="guide-turn" key={turn.id} aria-label={`Question: ${turn.question}`}>
            <div className="guide-question"><span>You</span><p>{turn.question}</p></div>
            <div className="guide-answer"><span className="guide-answer-label"><BookOpenText size={15} aria-hidden="true" />{turn.searchOnly ? "Source search" : "AML assistant"}</span>
              {turn.error ? <div role="alert"><p>{turn.error}</p><button className="guide-text-button" disabled={busy} onClick={() => { setQuestion(turn.question); composer.current?.focus(); }}>Edit and retry</button></div> : !turn.reply ? <p className="guide-pending" role="status"><LoaderCircle className="spin" size={16} aria-hidden="true" />{turn.searchOnly ? "Finding passages…" : "Retrieving sources and preparing an answer…"}</p> : <>
                {turn.reply.message ? <p>{turn.reply.message}</p> : null}
                {turn.reply.paragraphs.map((paragraph, index) => <CitedParagraph key={index} paragraph={paragraph} sources={turn.reply!.sources} onSource={showSource} />)}
                {turn.reply.sources.length ? <details className="guide-passages" open={turn.searchOnly || undefined}><summary>{turn.reply.sources.length} retrieved passages</summary>{turn.reply.sources.map(source => <button key={source.id} onClick={() => showSource(source)}><strong>[{source.number}] {source.references[0].heading.split(" / ").at(-1)}</strong><small>{source.references[0].path}</small><span>{source.text.slice(0, 170)}…</span></button>)}</details> : null}
              </>}
            </div>
          </article>)}
          <div ref={latest} />
        </div>
        <form className="guide-composer" onSubmit={submit}>
          <label htmlFor="guide-question" className="sr-only">Question about AML documentation</label>
          <Textarea ref={composer} id="guide-question" value={question} maxLength={2000} onChange={event => setQuestion(event.target.value)} placeholder="Ask about Red, Blue, agent lifecycles, training…" rows={3} disabled={!status} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (!busy) void ask(question, !status?.ready); } }} />
          <div className="guide-composer-actions"><span>Enter to send · Shift + Enter for a new line</span><div>{busy ? <Button type="button" variant="outline" onClick={() => pending.current?.abort()}><Square aria-hidden="true" /> Stop</Button> : <>{status?.ready ? <Button type="button" variant="outline" disabled={question.trim().length < 2} onClick={() => void ask(question, true)}><Search aria-hidden="true" /> Search only</Button> : null}<Button type="submit" disabled={!status || question.trim().length < 2}><Send aria-hidden="true" />{status?.ready ? "Ask AML" : "Search sources"}</Button></>}</div></div>
        </form>
        <p className="guide-chat-footnote">Documentation answers describe the indexed snapshot. They do not inspect live experiments. Conversations stay in this tab; submitted questions and retrieved passages go to the configured model.</p>
      </div>
      <aside className={`surface guide-sources ${sourcesOpen ? "guide-sources--open" : ""}`} id="guide-sources" aria-label="Source evidence">
        <div className="guide-source-heading"><h3>{selected ? "Source passage" : "Knowledge sources"}</h3>{selected ? <button onClick={() => setSelected(null)}>All sources</button> : null}</div>
        {selected ? <><h4>{selected.references[0].heading}</h4><p className="guide-source-path">{selected.references[0].path} · {selected.references[0].anchor} · part {selected.references[0].part}</p><pre className="guide-source-text" tabIndex={0}>{selected.text}</pre><details><summary>Provenance</summary><p>Source SHA-256</p><code>{selected.references[0].sha256}</code><p>Chunk ID</p><code>{selected.id}</code>{selected.references.length > 1 ? <p>Also appears in {selected.references.slice(1).map(ref => ref.path).join(", ")}.</p> : null}</details></> : <><p>Architecture guide and directly linked repository files. Source passages include their headings and checksums.</p><div className="guide-source-list" tabIndex={0} aria-label="Indexed documents">{status?.sources.map(source => <div key={source.path}><BookOpenText size={14} aria-hidden="true" /><span>{source.path}</span></div>)}</div>{status ? <details><summary>Index details</summary><p>{status.chunks} deduplicated passages · {status.retrieval} retrieval</p><code>{status.corpus_sha256}</code></details> : null}</>}
      </aside>
    </div>
  </section>;
}
