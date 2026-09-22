import { Card, Badge, ListGroup, Row, Col, Button } from "react-bootstrap";
import {
  ArrowRight,
  Users,
  FileCheck,
  ListChecks,
  Database,
  CheckCircle,
} from "lucide-react";

const typeVariant = {
  positive: "success",
  negative: "danger",
  edge: "warning",
};

export function RequirementStep({
  requirement,
  approved,
  onApprove,
  selectedScenario,
  onSelectScenario,
}) {
  if (!requirement) {
    return (
      <Card>
        <Card.Body>
          <Card.Title>Requirement</Card.Title>
          <Card.Subtitle className="text-muted">
            Upload a BRD first
          </Card.Subtitle>
        </Card.Body>
      </Card>
    );
  }

  const stats = [
    { icon: Users, value: 1, label: "User Stories" },
    {
      icon: FileCheck,
      value: requirement.businessRules?.length || 0,
      label: "Business Rules",
    },
    {
      icon: ListChecks,
      value: requirement.scenarios?.length || 0,
      label: "Scenarios",
    },
    {
      icon: Database,
      value: requirement.dataEntities?.length || 0,
      label: "Data Entities",
    },
  ];

  return (
    <div className="d-flex flex-column gap-4">
      <Card>
        <Card.Header className="d-flex justify-content-between align-items-center">
          <span>Requirements extracted</span>
          <div className="d-flex align-items-center gap-2">
            <Badge bg={approved ? "success" : "warning"}>
              {approved ? "Review approved" : "Review required"}
            </Badge>
            {!approved && (
              <Button size="sm" onClick={onApprove}>
                <CheckCircle size={14} className="me-1" />
                Approve requirements
              </Button>
            )}
          </div>
        </Card.Header>
        <Card.Body>
          <Row className="g-3">
            {stats.map((s) => (
              <Col md={3} key={s.label}>
                <div className="stat-card text-center">
                  <div className="value">{s.value}</div>
                  <div className="label">{s.label}</div>
                </div>
              </Col>
            ))}
          </Row>
        </Card.Body>
      </Card>

      <Row className="g-4">
        <Col lg={6}>
          <Card className="h-100">
            <Card.Header>Overview</Card.Header>
            <Card.Body>
              <div className="mb-3">
                <div className="text-uppercase small fw-bold text-muted">
                  Key
                </div>
                <div className="fw-semibold">{requirement.key}</div>
              </div>
              <div className="mb-3">
                <div className="text-uppercase small fw-bold text-muted">
                  Summary
                </div>
                <div>{requirement.summary}</div>
              </div>
              <div>
                <div className="text-uppercase small fw-bold text-muted">
                  User Story
                </div>
                <div>{requirement.userStory}</div>
              </div>
            </Card.Body>
          </Card>
        </Col>

        <Col lg={6}>
          <Card className="h-100">
            <Card.Header>Key Insights</Card.Header>
            <Card.Body>
              <ListGroup variant="flush">
                <ListGroup.Item className="d-flex align-items-start gap-2 px-0">
                  <Badge bg="dark" className="mt-1">
                    Entities
                  </Badge>
                  <div className="d-flex flex-wrap gap-1">
                    {requirement.dataEntities?.map((e) => (
                      <Badge key={e} bg="secondary">
                        {e}
                      </Badge>
                    ))}
                  </div>
                </ListGroup.Item>
                <ListGroup.Item className="d-flex align-items-start gap-2 px-0">
                  <Badge bg="dark" className="mt-1">
                    Rules
                  </Badge>
                  <div className="text-muted small">
                    {requirement.businessRules?.map((r) => r).join("; ")}
                  </div>
                </ListGroup.Item>
                <ListGroup.Item className="d-flex align-items-start gap-2 px-0">
                  <Badge bg="dark" className="mt-1">
                    Acceptance
                  </Badge>
                  <div className="text-muted small">
                    {requirement.acceptanceCriteria?.join("; ") ||
                      "No acceptance criteria extracted"}
                  </div>
                </ListGroup.Item>
              </ListGroup>
            </Card.Body>
          </Card>
        </Col>
      </Row>

      <Card>
        <Card.Header className="d-flex justify-content-between align-items-center">
          <span>Business Rules</span>
          {!approved && <span className="text-muted small">Approve the review before continuing</span>}
        </Card.Header>
        <ListGroup variant="flush">
          {requirement.businessRules?.map((rule, i) => (
            <ListGroup.Item key={i} className="d-flex align-items-center gap-2">
              <Badge bg="primary">{i + 1}</Badge>
              {rule}
            </ListGroup.Item>
          ))}
        </ListGroup>
      </Card>

      <Card>
        <Card.Header className="d-flex justify-content-between align-items-center">
          <span>Scenarios</span>
          <span className="text-muted small">Select one to continue</span>
        </Card.Header>
        <Card.Body>
          <ListGroup>
            {requirement.scenarios?.map((scenario) => (
              <ListGroup.Item
                key={scenario.key}
                action
                active={selectedScenario === scenario.key}
                onClick={() => approved && onSelectScenario(scenario.key)}
                disabled={!approved}
                className="d-flex flex-column gap-2"
              >
                <div className="d-flex justify-content-between align-items-center">
                  <div>
                    <div className="fw-semibold">{scenario.name}</div>
                    <div className="small text-muted">{scenario.key}</div>
                  </div>
                  <div className="d-flex gap-2 align-items-center">
                    <Badge bg={typeVariant[scenario.type] || "secondary"}>
                      {scenario.type}
                    </Badge>
                    {selectedScenario === scenario.key && (
                      <Button
                        size="sm"
                        onClick={(event) => {
                          event.stopPropagation();
                          if (approved) onSelectScenario(scenario.key);
                        }}
                        disabled={!approved}
                      >
                        Continue to Data Recipe
                        <ArrowRight className="ms-2" size={14} />
                      </Button>
                    )}
                  </div>
                </div>
                <div className="small text-muted">{scenario.description}</div>
              </ListGroup.Item>
            ))}
          </ListGroup>
        </Card.Body>
      </Card>
    </div>
  );
}
