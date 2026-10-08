import { Children, forwardRef, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown } from "lucide-react";

function optionText(node) {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(optionText).join("");
  return node?.props?.children ? optionText(node.props.children) : "";
}

export const AppSelect = forwardRef(function AppSelect({ children, value = "", onChange, disabled = false, className = "", id, name, "aria-label": ariaLabel }, forwardedRef) {
  const rootRef = useRef(null);
  const internalButtonRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(0);
  const [menuStyle, setMenuStyle] = useState({});
  const options = useMemo(() => Children.toArray(children).filter((child) => child?.type === "option").map((child) => ({
    value: String(child.props.value ?? ""),
    label: child.props.children,
    text: optionText(child.props.children),
    disabled: Boolean(child.props.disabled),
  })), [children]);
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === String(value ?? "")));
  const selected = options[selectedIndex];

  function assignButtonRef(node) {
    internalButtonRef.current = node;
    if (typeof forwardedRef === "function") forwardedRef(node);
    else if (forwardedRef) forwardedRef.current = node;
  }

  function positionMenu() {
    const rect = internalButtonRef.current?.getBoundingClientRect();
    if (!rect) return;
    const menuHeight = Math.min(options.length, 4) * 40 + 10;
    const opensUp = window.innerHeight - rect.bottom < menuHeight + 12 && rect.top > menuHeight + 12;
    setMenuStyle({ left: rect.left, top: opensUp ? rect.top - menuHeight - 6 : rect.bottom + 6, width: rect.width });
  }

  function showMenu() {
    if (disabled) return;
    positionMenu();
    setHighlightedIndex(selectedIndex);
    setOpen(true);
  }

  function choose(option) {
    if (option.disabled) return;
    onChange?.({ target: { value: option.value, name }, currentTarget: { value: option.value, name } });
    setOpen(false);
    requestAnimationFrame(() => internalButtonRef.current?.focus());
  }

  function handleKeyDown(event) {
    if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) event.preventDefault();
    if (!open) {
      if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) showMenu();
      return;
    }
    if (event.key === "Escape" || event.key === "Tab") { setOpen(false); return; }
    if (event.key === "ArrowDown") setHighlightedIndex((index) => Math.min(options.length - 1, index + 1));
    if (event.key === "ArrowUp") setHighlightedIndex((index) => Math.max(0, index - 1));
    if (event.key === "Enter" || event.key === " ") choose(options[highlightedIndex]);
  }

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsideClick = (event) => {
      if (!rootRef.current?.contains(event.target) && !event.target.closest?.(".app-select-menu")) setOpen(false);
    };
    const closeOnViewportChange = (event) => {
      if (event.type === "scroll" && event.target?.closest?.(".app-select-menu")) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    window.addEventListener("resize", closeOnViewportChange);
    window.addEventListener("scroll", closeOnViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideClick);
      window.removeEventListener("resize", closeOnViewportChange);
      window.removeEventListener("scroll", closeOnViewportChange, true);
    };
  }, [open]);

  return <div ref={rootRef} className={`app-select ${open ? "is-open" : ""} ${disabled ? "is-disabled" : ""} ${className}`}>
    <button ref={assignButtonRef} id={id} type="button" className="app-select-trigger" aria-label={ariaLabel} aria-haspopup="listbox" aria-expanded={open} disabled={disabled} onClick={() => open ? setOpen(false) : showMenu()} onKeyDown={handleKeyDown}>
      <span title={selected?.text}>{selected?.label || "Select"}</span><ChevronDown size={16} aria-hidden="true" />
    </button>
    {open && createPortal(<div className="app-select-menu" style={menuStyle} role="listbox" aria-label={ariaLabel}>
      {options.map((option, index) => <button key={`${option.value}-${index}`} type="button" role="option" aria-selected={option.value === String(value ?? "")} disabled={option.disabled} className={`app-select-option ${index === highlightedIndex ? "is-highlighted" : ""}`} onPointerMove={() => setHighlightedIndex(index)} onClick={() => choose(option)}><span>{option.label}</span>{option.value === String(value ?? "") && <Check size={15} aria-hidden="true" />}</button>)}
    </div>, document.body)}
  </div>;
});
