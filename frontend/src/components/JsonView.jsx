export function JsonView({ data }) {
  return (
    <pre className="mb-0" style={{ maxHeight: '24rem', overflow: 'auto' }}>
      {JSON.stringify(data, null, 2)}
    </pre>
  )
}
