import { Sparkles } from "lucide-react";

export function WorkspacePageHero({ productLabel = "TestIQ Workspace", eyebrow, title, cues = [], context, children }) {
  return <section className="workspace-page-hero">
    <div className="workspace-page-hero-copy">
      <div className="workspace-page-hero-meta"><span><Sparkles size={12} /> {productLabel}</span><div className="eyebrow">{eyebrow}</div></div>
      <h1>{title}</h1>
      {cues.length > 0 && <div className="workspace-page-hero-cues">{cues.map((cue) => <span key={cue}>{cue}</span>)}</div>}
    </div>
    {(context || children) && <div className="workspace-page-hero-actions">{context && <span className="workspace-page-context">{context}</span>}{children}</div>}
  </section>;
}
