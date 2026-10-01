import { useState } from 'react'
import { ClipboardCheck, CheckCircle, ArrowRight } from 'lucide-react'
import { Alert, Badge, Button, Card, Form, ListGroup } from 'react-bootstrap'
import { generateTestCases } from '../api/client.js'
import { LoadingSpinner } from './LoadingSpinner.jsx'

export function TestCaseStep({ requirement, testCases, setTestCases, onApprove }) {
  const [selected, setSelected] = useState(new Set())
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function handleGenerate() {
    if (!requirement?.key) return
    setLoading(true); setError(null)
    try {
      const result = await generateTestCases(requirement.key)
      setTestCases(result.test_cases)
      setSelected(new Set(result.test_cases.map((testCase) => testCase.case_id)))
    } catch (err) { setError(err.message) }
    finally { setLoading(false) }
  }

  function toggle(caseId) {
    const next = new Set(selected)
    if (next.has(caseId)) next.delete(caseId); else next.add(caseId)
    setSelected(next)
  }

  function approve() {
    const approved = testCases.filter((testCase) => selected.has(testCase.case_id))
    if (approved.length) onApprove(approved)
  }

  return <div className="d-flex flex-column gap-4">
    <Card><Card.Header className="d-flex justify-content-between align-items-center"><span>Generate Test Cases</span><Badge bg={testCases.length ? 'success' : 'warning'}>{testCases.length ? `${testCases.length} generated` : 'Not generated'}</Badge></Card.Header><Card.Body>
      <p className="text-muted">Create formal test cases from the approved requirement scenarios before generating test data.</p>
      <Button onClick={handleGenerate} disabled={!requirement || loading}>{loading ? <LoadingSpinner variant="inline" label="Generating" /> : <><ClipboardCheck size={16} className="me-2" />Generate Test Cases</>}</Button>
      {error && <Alert variant="danger" className="mt-3">{error}</Alert>}
    </Card.Body></Card>
    {testCases.length > 0 && <Card><Card.Header>Select cases for this data-generation run</Card.Header><ListGroup variant="flush">{testCases.map((testCase) => <ListGroup.Item key={testCase.case_id}><Form.Check type="checkbox" checked={selected.has(testCase.case_id)} onChange={() => toggle(testCase.case_id)} label={<span><strong>{testCase.case_id}: {testCase.title}</strong> <Badge bg={testCase.type === 'positive' ? 'success' : 'warning'}>{testCase.type}</Badge></span>} /><div className="small text-muted ms-4 mt-2">{testCase.description}</div><div className="small mt-2 ms-4"><strong>Expected:</strong> {testCase.expected_result}</div><div className="small text-muted ms-4">Entities: {testCase.data_entities.join(', ')}</div></ListGroup.Item>)}</ListGroup><Card.Body className="d-flex justify-content-end"><Button onClick={approve} disabled={!selected.size}><CheckCircle size={16} className="me-2" />Approve {selected.size} Test Case{selected.size === 1 ? '' : 's'} & Continue<ArrowRight size={16} className="ms-2" /></Button></Card.Body></Card>}
  </div>
}
