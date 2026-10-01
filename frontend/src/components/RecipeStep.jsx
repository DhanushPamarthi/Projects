import { useState } from 'react'
import { ChefHat, AlertCircle, ArrowRight, Users, Link2, CheckCircle } from 'lucide-react'
import { Card, Button, Badge, Alert, Row, Col, Table as BTable, ListGroup } from 'react-bootstrap'
import { buildRecipe, saveRecipe } from '../api/client.js'
import { LoadingSpinner } from './LoadingSpinner.jsx'

export function RecipeStep({ scenarioKey, recipe, setRecipe }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [draftRecipe, setDraftRecipe] = useState(null)

  async function handleBuild() {
    if (!scenarioKey) return
    setLoading(true); setError(null)
    try { const data = await buildRecipe(scenarioKey); setDraftRecipe(data.recipe) }
    catch (err) { setError(err.message) }
    finally { setLoading(false) }
  }

  async function handleApprove() {
    if (!scenarioKey || !draftRecipe) return
    setLoading(true); setError(null)
    try { const data = await saveRecipe(scenarioKey, draftRecipe); setRecipe(data.recipe) }
    catch (err) { setError(err.message) }
    finally { setLoading(false) }
  }

  const displayedRecipe = draftRecipe || recipe

  function updateCount(index, value) {
    const count = Math.max(1, Math.min(10000, Number(value) || 1))
    setDraftRecipe({ ...displayedRecipe, entities: displayedRecipe.entities.map((entity, entityIndex) => entityIndex === index ? { ...entity, count } : entity) })
  }

  return (
    <div className="d-flex flex-column gap-4">
      <Card>
        <Card.Header className="d-flex justify-content-between align-items-center">
          <span>Build Data Recipe</span>
          {recipe && <Badge bg="success"><CheckCircle size={14} className="me-1" />Approved</Badge>}
          {!recipe && draftRecipe && <Badge bg="warning">Review required</Badge>}
        </Card.Header>
        <Card.Body>
          <Card.Subtitle className="mb-3 text-muted">Review and customize the data recipe for your scenario</Card.Subtitle>
          <div className="d-flex align-items-center gap-2 mb-3"><span className="text-muted">Scenario:</span><Badge bg="primary">{scenarioKey || 'None selected'}</Badge></div>
          <Button onClick={handleBuild} disabled={!scenarioKey || loading}>{loading ? <LoadingSpinner variant="inline" label="Building recipe" /> : <><ChefHat className="me-2" size={16} />Generate Recipe</>}</Button>
          {error && <Alert variant="danger" className="mt-3">{error}</Alert>}
        </Card.Body>
      </Card>

      {displayedRecipe && <>
        <Row className="g-4">
          <Col md={8}><Card className="h-100"><Card.Header>{displayedRecipe.scenario_name}</Card.Header><Card.Body>
            <p className="text-muted">{displayedRecipe.description}</p>
            <div className="d-flex gap-2 mb-4"><Badge bg="dark">{displayedRecipe.scenario_type}</Badge><Badge bg="secondary">{displayedRecipe.scenario_key}</Badge></div>
            <h6 className="fw-bold">Required Entities</h6>
            <Row className="g-2 mb-4">{displayedRecipe.entities?.map((entity, index) => <Col md={4} key={entity.table}><div className="d-flex align-items-center gap-2 p-2 border rounded-3 bg-light"><Users size={18} className="text-muted" /><div className="w-100"><div className="fw-semibold text-capitalize">{entity.table}</div><input className="form-control form-control-sm mt-1" type="number" min="1" max="10000" value={entity.count} aria-label={`${entity.table} record count`} onChange={(event) => updateCount(index, event.target.value)} /></div></div></Col>)}</Row>
            <h6 className="fw-bold">Relationships</h6>
            <ListGroup variant="flush">{displayedRecipe.references?.map((ref, index) => <ListGroup.Item key={index} className="d-flex align-items-center gap-2 px-0"><Link2 size={16} className="text-muted" /><Badge bg="dark">{ref.from_table}.{ref.from_column}</Badge><span className="text-muted">→ references →</span><Badge bg="secondary">{ref.to_table}.{ref.to_column}</Badge></ListGroup.Item>)}</ListGroup>
          </Card.Body></Card></Col>
          <Col md={4}><Card className="h-100"><Card.Header>Applied Rules</Card.Header><ListGroup variant="flush">{displayedRecipe.rules?.map((rule, index) => <ListGroup.Item key={index} className="d-flex align-items-center gap-2 px-0"><AlertCircle size={16} style={{ color: 'var(--brand-orange)' }} /><div><div className="fw-semibold small">{rule.table}.{rule.column}</div><div className="text-muted small">{rule.operator} {Array.isArray(rule.value) ? rule.value.join('/') : String(rule.value)}</div></div></ListGroup.Item>)}</ListGroup></Card></Col>
        </Row>
        <Card><Card.Header>Recipe Details</Card.Header><Card.Body><div className="table-responsive"><BTable striped bordered hover size="sm"><thead><tr><th>Table</th><th>Records</th><th>Columns</th><th>Key Constraints</th></tr></thead><tbody>{displayedRecipe.entities?.map((entity) => <tr key={entity.table}><td className="text-capitalize fw-semibold">{entity.table}</td><td>{entity.count}</td><td>{entity.columns?.map((column) => column.name).join(', ')}</td><td>{displayedRecipe.rules?.filter((rule) => rule.table === entity.table).map((rule) => `${rule.column} ${rule.operator} ${Array.isArray(rule.value) ? rule.value.join('/') : rule.value}`).join('; ') || '-'}</td></tr>)}</tbody></BTable></div></Card.Body></Card>
        <div className="d-flex justify-content-end"><Button onClick={handleApprove} disabled={loading || Boolean(recipe && !draftRecipe)}>{recipe ? 'Recipe Approved' : 'Approve Recipe & Continue to Generate'}<ArrowRight className="ms-2" size={16} /></Button></div>
      </>}
    </div>
  )
}
