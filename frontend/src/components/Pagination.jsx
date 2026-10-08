import { ChevronLeft, ChevronRight } from "lucide-react";

export function Pagination({ page, pageSize, totalItems, onPageChange, label = "records" }) {
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
  if (totalPages <= 1) return null;
  const start = Math.max(1, Math.min(page - 2, totalPages - 4));
  const pages = Array.from({ length: Math.min(5, totalPages) }, (_, index) => start + index);
  return <nav className="app-pagination" aria-label={`${label} pages`}>
    <span>Showing {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, totalItems)} of {totalItems}</span>
    <div><button type="button" aria-label={`Previous ${label} page`} disabled={page === 1} onClick={() => onPageChange(page - 1)}><ChevronLeft size={16} /></button>{pages.map((pageNumber) => <button type="button" className={page === pageNumber ? "active" : ""} aria-current={page === pageNumber ? "page" : undefined} key={pageNumber} onClick={() => onPageChange(pageNumber)}>{pageNumber}</button>)}<button type="button" aria-label={`Next ${label} page`} disabled={page === totalPages} onClick={() => onPageChange(page + 1)}><ChevronRight size={16} /></button></div>
  </nav>;
}
