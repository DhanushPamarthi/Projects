import { useState } from 'react'
import { ShieldCheck, CheckCircle, XCircle, Download, RefreshCw } from 'lucide-react'
import { Card, Button, Badge, Alert, Row, Col, ListGroup } from 'react-bootstrap'
import { validateData, exportData, generateData, generateSelectedTestCases } from '../api/client.js'

function downloadCsv(filename, csv) {
  const blob = new Blob([csv], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function validationReportCsv(result) {
  const lines = [
    'Validation Report',
    `Scenario,${result.recipe_key}`,
    `Records checked,${result.record_count}`,
    `Errors,${result.error_count}`,
    `Status,${result.status}`,
    '',
    'Table,Message',
  ]
  result.errors?.forEach((err) => lines.push(`${err.table},"${err.message}"`))
  return lines.join('\n')
}

export function ValidateStep({ scenarioKey, scenarioKeys = [], result, setResult }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function handleValidate() {
    if (!scenarioKey) return
    setLoading(true)
    setError(null)
    try {
      const keys = [...new Set(scenarioKeys.length ? scenarioKeys : [scenarioKey])]
      const results = await Promise.all(keys.map((key) => validateData(key)))
      if (results.length === 1) setResult(results[0])
      else setResult({ status: results.every((item) => item.status === 'valid') ? 'valid' : 'invalid', record_count: results.reduce((sum, item) => sum + (item.record_count || 0), 0), error_count: results.reduce((sum, item) => sum + (item.error_count || 0), 0), errors: results.flatMap((item) => item.errors || []), checks: results.reduce((all, item) => { Object.entries(item.checks || {}).forEach(([key, value]) => { all[key] = all[key] || { passed: 0, failed: 0 }; all[key].passed += value.passed; all[key].failed += value.failed }); return all }, {}), recipe_key: keys.join(', ') })
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleRegenerate() {
    if (!scenarioKey) return
    setLoading(true)
    setError(null)
    try {
      const keys = [...new Set(scenarioKeys.length ? scenarioKeys : [scenarioKey])]
      if (keys.length > 1) await generateSelectedTestCases(keys, 1, {})
      else await generateData(scenarioKey, 1)
      await handleValidate()
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleExportCsv() {
    try {
      const keys = [...new Set(scenarioKeys.length ? scenarioKeys : [scenarioKey])]
      const exports = await Promise.all(keys.map((key) => exportData(key)))
      exports.forEach((data, index) => Object.entries(data.csv).forEach(([table, csv]) => downloadCsv(`${keys[index]}-${table}.csv`, csv)))
    } catch (err) {
      setError(err.message)
    }
  }

  function handleExportReport() {
    if (!result) return
    downloadCsv('validation_report.csv', validationReportCsv(result))
  }

  const isValid = result?.status === 'valid'
  const passRate = result?.record_count
    ? Math.round(((result.record_count - result.error_count) / result.record_count) * 100)
    : 0

  const checks = [
    { label: 'Business Rules', key: 'business_rules' },
    { label: 'Referential Integrity', key: 'referential_integrity' },
    { label: 'Required Fields', key: 'required_fields' },
    { label: 'Domain Validation', key: 'domain_validation' },
    { label: 'Data Types', key: 'data_types' },
  ]

  return (
    <div className="d-flex flex-column gap-4">
      <Card>
        <Card.Header>Validate Generated Data</Card.Header>
        <Card.Body>
          <div className="d-flex align-items-center gap-2 mb-3">
            <span className="text-muted">Scenario:</span>
            <Badge bg="primary">{scenarioKey || 'None selected'}</Badge>
          </div>
          <Button onClick={handleValidate} disabled={!scenarioKey || loading}>
            <ShieldCheck className="me-2" size={16} />
            {loading ? 'Validating...' : 'Validate Records'}
          </Button>
          {error && <Alert variant="danger" className="mt-3">{error}</Alert>}
        </Card.Body>
      </Card>

      {result && (
        <>
          <Card>
            <Card.Header>Validation Results</Card.Header>
            <Card.Body>
              <div className="text-muted small mb-3">
                Run: {result.generation_run_id || 'n/a'} · Seed: {result.seed ?? 'n/a'} · Generated: {result.generated_at || 'n/a'}
              </div>
              <Row className="g-4 align-items-center">
                <Col md={4} className="text-center">
                  <svg width="140" height="140" viewBox="0 0 140 140">
                    <circle cx="70" cy="70" r="60" fill="none" stroke="var(--navy-100)" strokeWidth="12" />
                    <circle
                      cx="70"
                      cy="70"
                      r="60"
                      fill="none"
                      stroke={isValid ? 'var(--signal-positive)' : 'var(--signal-negative)'}
                      strokeWidth="12"
                      strokeDasharray={`${(passRate / 100) * 377} 377`}
                      strokeLinecap="round"
                      transform="rotate(-90 70 70)"
                    />
                    <text x="70" y="70" textAnchor="middle" dominantBaseline="middle" className="fw-bold" fontSize="28" fill="var(--brand-navy)">
                      {passRate}%
                    </text>
                    <text x="70" y="88" textAnchor="middle" fontSize="10" fill="var(--navy-500)">
                      {isValid ? 'Records valid' : 'Records invalid'}
                    </text>
                  </svg>
                </Col>
                <Col md={8}>
                  <div className="d-flex align-items-center gap-3 mb-3">
                    <div
                      className="rounded-circle d-flex align-items-center justify-content-center"
                      style={{
                        width: 48,
                        height: 48,
                        background: isValid ? 'rgba(45, 156, 91, 0.1)' : 'rgba(212, 64, 59, 0.1)',
                      }}
                    >
                      {isValid ? <CheckCircle size={24} style={{ color: 'var(--signal-positive)' }} /> : <XCircle size={24} style={{ color: 'var(--signal-negative)' }} />}
                    </div>
                    <div>
                      <h5 className="mb-0 text-capitalize fw-bold">{result.status}</h5>
                      <p className="text-muted small mb-0">
                        {result.record_count} records checked · {result.error_count} errors
                      </p>
                    </div>
                    <Badge bg={isValid ? 'success' : 'danger'} className="ms-auto">{result.status}</Badge>
                  </div>

                  <ListGroup>
                    {checks.map((check) => (
                      <ListGroup.Item key={check.label} className="d-flex justify-content-between align-items-center">
                        <span>{check.label}</span>
                         {(() => {
                           const checkResult = result.checks?.[check.key]
                           const pass = checkResult ? checkResult.failed === 0 : isValid
                           return <Badge bg={pass ? 'success' : 'danger'}>{pass ? 'Passed' : 'Failed'} ({checkResult?.failed || 0})</Badge>
                         })()}
                      </ListGroup.Item>
                    ))}
                  </ListGroup>
                </Col>
              </Row>

              {result.errors?.length > 0 && (
                <div className="d-flex flex-column gap-2 mt-4">
                  {result.errors.map((err, i) => (
                    <Alert variant="danger" key={i} className="py-2 mb-0">
                      <span className="fw-semibold">{err.table}</span> · {err.message}
                    </Alert>
                  ))}
                </div>
              )}
            </Card.Body>
          </Card>

          <div className="d-flex justify-content-end gap-2">
             <Button variant="outline-primary" onClick={handleRegenerate} disabled={loading}>
              <RefreshCw className="me-2" size={16} />
              Generate Again
            </Button>
            <Button variant="outline-primary" onClick={handleExportCsv}>
              <Download className="me-2" size={16} />
              Download CSV
            </Button>
            <Button onClick={handleExportReport}>
              <Download className="me-2" size={16} />
              Validation Report
            </Button>
          </div>
        </>
      )}
    </div>
  )
}
