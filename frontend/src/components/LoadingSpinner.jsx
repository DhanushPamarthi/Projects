export function LoadingSpinner({
  label = "Loading",
  detail = "",
  variant = "section",
  className = "",
}) {
  const classes = ["app-loading", `app-loading--${variant}`, className].filter(Boolean).join(" ");
  const Root = variant === "inline" ? "span" : "div";

  return (
    <Root className={classes} role="status" aria-live="polite" aria-busy="true" aria-label={label}>
      <span className="app-loading__spinner" aria-hidden="true"><span /></span>
      {(label || detail) && <span className="app-loading__copy">{label && <strong>{label}</strong>}{detail && <span>{detail}</span>}</span>}
    </Root>
  );
}
