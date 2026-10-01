import { useState } from 'react'
import { Database, Check, Table, Download } from 'lucide-react'
import { Card, Button, Table as BTable, Badge, Alert, Form, Row, Col, ListGroup } from 'react-bootstrap'
import { generateData, generateSelectedTestCases, exportData, listGenerationRuns } from '../api/client.js'
import { LoadingSpinner } from './LoadingSpinner.jsx'

function recordsToCsv(records) {
  if (!records?.length) return ''
  const headers = Object.keys(records[0])
  const rows = records.map((r) =>
    headers.map((h) => {
      const v = r[h]
      return typeof v === 'string' && v.includes(',') ? `"${v}"` : String(v)
    }).join(',')
  )
  return [headers.join(','), ...rows].join('\n')
}

function downloadCsv(filename, csv) {
  const blob = new Blob([csv], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function GenerateStep({ scenarioKey, scenarioKeys = [], recipe, result, setResult }) {
  const [recordCount, setRecordCount] = useState(1)
  const [options, setOptions] = useState({
    referentialIntegrity: true,
    businessRules: true,
    realisticValues: true,
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [runs, setRuns] = useState([])

  async function handleGenerate() {
    if (!scenarioKey) return
    setLoading(true)
    setError(null)
    try {
      const keys = [...new Set(scenarioKeys.length ? scenarioKeys : [scenarioKey])]
      const data = keys.length > 1
        ? await generateSelectedTestCases(keys, recordCount, options)
        : await generateData(scenarioKey, recordCount, options)
      if (data.runs) {
        const records = {}
        data.runs.forEach((run) => Object.entries(run.records).forEach(([table, rows]) => { records[`${run.scenario_key}:${table}`] = rows }))
        setResult({ ...data, records, record_count: data.runs.reduce((total, run) => total + run.record_count, 0), generation_run_id: data.runs.map((run) => run.generation_run_id).join(', '), seed: data.runs.map((run) => run.seed).join(', ') })
      } else {
        setResult(data)
      }
      setRuns((await Promise.all(keys.map((key) => listGenerationRuns(key)))).flat())
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleExport() {
    try {
      const keys = [...new Set(scenarioKeys.length ? scenarioKeys : [scenarioKey])]
      const exports = await Promise.all(keys.map((key) => exportData(key)))
      exports.forEach((data, index) => Object.entries(data.csv).forEach(([table, csv]) => downloadCsv(`${keys[index]}-${table}.csv`, csv)))
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="d-flex flex-column gap-4">
      <Card>
        <Card.Header>Generate Synthetic Test Data</Card.Header>
        <Card.Body>
          <Card.Subtitle className="mb-3 text-muted">
            Create realistic, rule-based test data using the configured recipe
          </Card.Subtitle>

          <Row className="g-4">
            <Col md={6}>
              <div className="mb-3">
                <Form.Label className="fw-semibold">Scenario</Form.Label>
                <Badge bg="primary">{scenarioKey || 'None selected'}</Badge>
              </div>
              <div className="mb-3">
                <Form.Label className="fw-semibold">Number of records</Form.Label>
                <Form.Control
                  type="number"
                  min={1}
                  max={10000}
                  value={recordCount}
                  onChange={(e) => setRecordCount(parseInt(e.target.value, 10) || 1)}
                  style={{ maxWidth: 200 }}
                />
              </div>
              <Button onClick={handleGenerate} disabled={!scenarioKey || loading}>
                <Database className="me-2" size={16} />
                {loading ? <LoadingSpinner variant="inline" label="Generating" /> : 'Generate Data'}
              </Button>
            </Col>

            <Col md={6}>
              <Form.Label className="fw-semibold">Options</Form.Label>
              <Form.Check
                type="checkbox"
                label="Preserve referential integrity"
                checked={options.referentialIntegrity}
                onChange={(e) => setOptions({ ...options, referentialIntegrity: e.target.checked })}
              />
              <Form.Check
                type="checkbox"
                label="Apply all business rules"
                checked={options.businessRules}
                onChange={(e) => setOptions({ ...options, businessRules: e.target.checked })}
              />
              <Form.Check
                type="checkbox"
                label="Generate realistic values"
                checked={options.realisticValues}
                onChange={(e) => setOptions({ ...options, realisticValues: e.target.checked })}
              />
            </Col>
          </Row>

          {error && <Alert variant="danger" className="mt-3">{error}</Alert>}
        </Card.Body>
      </Card>

      {result && (
        <Card>
          <Card.Header className="d-flex justify-content-between align-items-center">
            <span>
              <Table size={16} className="me-2" />
              Generated Records
            </span>
            <span className="text-muted small">
              {result.record_count} records generated successfully
            </span>
            <span className="text-muted small">Run: {result.generation_run_id || 'n/a'} · Seed: {result.seed ?? 'random'}</span>
          </Card.Header>
          <Card.Body>
            {runs.length > 0 && <div className="mb-3"><div className="fw-semibold mb-2">Generation history</div><ListGroup>{runs.slice(0, 5).map((run) => <ListGroup.Item key={run.generation_run_id} className="d-flex justify-content-between"><span>{run.generation_run_id}</span><span className="text-muted small">{run.record_count} records · seed {run.seed} · {run.generated_at}</span></ListGroup.Item>)}</ListGroup></div>}
            <Row className="g-3 mb-3">
              {Object.entries(result.records).map(([table, rows]) => (
                <Col md={4} key={table}>
                  <div className="stat-card text-center">
                    <div className="value">{rows.length}</div>
                    <div className="label text-capitalize">{table}</div>
                  </div>
                </Col>
              ))}
            </Row>

            {Object.entries(result.records).map(([table, rows]) => (
              <div key={table} className="mb-4">
                <div className="d-flex align-items-center gap-2 mb-2">
                  <h5 className="mb-0 text-capitalize">{table}</h5>
                  <Badge bg="dark">{rows.length} rows</Badge>
                </div>
                <div className="table-responsive">
                  <BTable striped bordered hover size="sm">
                    <thead>
                      <tr>
                        {Object.keys(rows[0] || {}).map((col) => (
                          <th key={col} className="text-capitalize">{col.replace(/_/g, ' ')}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row, i) => (
                        <tr key={i}>
                          {Object.values(row).map((val, j) => (
                            <td key={j}>
                              {typeof val === 'boolean' ? (val ? 'true' : 'false') : String(val)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </BTable>
                </div>
              </div>
            ))}

            <div className="d-flex justify-content-end gap-2">
              <Button variant="outline-primary" onClick={handleExport}>
                <Download className="me-2" size={16} />
                Download CSV
              </Button>
              <Button onClick={() => setResult(result)}>
                Continue to Validate
                <Check className="ms-2" size={16} />
              </Button>
            </div>
          </Card.Body>
        </Card>
      )}
    </div>
  )
}
