import { useState } from "react";
import {
  FlaskConical,
  Lock,
  Mail,
  ArrowRight,
  Eye,
  EyeOff,
  FileText,
  ChefHat,
  Database,
  ShieldCheck,
} from "lucide-react";
import {
  Card,
  Button,
  Form,
  Alert,
  Row,
  Col,
  Container,
} from "react-bootstrap";
import styles from "./LoginPage.module.css";
import { registerUser } from "../api/client.js";
import { toast } from "react-toastify";

const FEATURES = [
  {
    icon: FileText,
    title: "Understand business requirements",
    description: "Extract user stories, rules and entities",
  },
  {
    icon: ChefHat,
    title: "Build data recipes",
    description: "Map to CDP data model",
  },
  {
    icon: Database,
    title: "Generate realistic data",
    description: "Rule-based, referentially consistent",
  },
  {
    icon: ShieldCheck,
    title: "Validate with confidence",
    description: "Ensure data meets business rules",
  },
];

const BRAND_NAVY = "#192B37";
const BRAND_ORANGE = "#FF5640";

function BrandHeader() {
  return (
    <div className={styles.brandHeader}>
      <div className={styles.brandIdentity}>
        <div className={styles.logoBox}>
          <FlaskConical color="white" size={24} />
        </div>

        <div>
          <div className={styles.logoText}>
            Test<span style={{ color: BRAND_ORANGE }}>IQ</span>
          </div>
          <div className={styles.logoSubtitle}>AI Test Data Generator</div>
        </div>
      </div>

      <div className={styles.headerTagline}>
        Turn requirements into reliable test data
      </div>
    </div>
  );
}

function FeatureItem({ icon: Icon, title, description }) {
  return (
    <div className={styles.featureItem}>
      <div className={styles.featureIconWrapper}>
        <Icon size={22} color="#5D7A8C" strokeWidth={1.8} />
      </div>
      <div>
        <div className={styles.featureTitle}>{title}</div>
        <div className={styles.featureDescription}>{description}</div>
      </div>
    </div>
  );
}

function ProductPanel() {
  return (
    <div className={styles.productPanel}>
      <div className={styles.heroCopy}>
        <h1 className={styles.heroTitle}>
          From requirements
          <br />
          to ready test data
        </h1>

        <p className={styles.heroSubtitle}>
          Upload your BRD, let AI understand the rules, and generate realistic,
          validated synthetic data for your CDP use cases.
        </p>

        <div className={styles.featureList}>
          {FEATURES.map((feature) => (
            <FeatureItem key={feature.title} {...feature} />
          ))}
        </div>

        <div className={styles.taglineFooter}>
          <span className={styles.orangeDash}>—</span>
          <span className={styles.taglineQuote}>
            "Better test data. Faster insights."
          </span>
        </div>
      </div>
      <img
        className={styles.workflowImage}
        src="/loginflow.png"
        alt="TestIQ workflow from requirements to validation"
      />
    </div>
  );
}

export function RegisterPage({ onBack }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [agreeTerms, setAgreeTerms] = useState(false);
  const [error, setError] = useState(null);
  const [isLoading, setIsLoading] = useState(false);

  function completeRegistration() {
    toast.success("Registration successful. Please sign in.");
    setTimeout(onBack, 250);
  }

  function handleSubmit(e) {
    e.preventDefault();
    setError(null);

    if (!email || !password || !confirmPassword) {
      setError("Please fill in all fields");
      return;
    }

    if (password !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }

    if (!agreeTerms) {
      setError("Please agree to the Terms of Service and Privacy Policy");
      return;
    }

    setIsLoading(true);
    registerUser(email, password)
      .then(() => { localStorage.removeItem("testiq_auth_token"); localStorage.removeItem("testiq_current_user"); completeRegistration(); })
      .catch(() => {
        const users = JSON.parse(localStorage.getItem("testiq_users") || "{}");
        if (users[email]) { setError("An account with this email already exists"); setIsLoading(false); return; }
        const user = { email, password };
        users[email] = user; localStorage.setItem("testiq_users", JSON.stringify(users)); localStorage.removeItem("testiq_current_user"); localStorage.removeItem("testiq_local_session"); completeRegistration(); setIsLoading(false);
      });
    return;

  }

  return (
    <div className={styles.page}>
      <BrandHeader />

      <div className={styles.main}>
        <Container fluid className={styles.container}>
          <Row className="g-4 align-items-center">
            <Col lg={7}>
              <ProductPanel />
            </Col>

            <Col lg={5}>
              <Card className={styles.formCard}>
                <Card.Body className={styles.formCardBody}>
                  <h3 className={styles.formTitle}>Create account</h3>
                  {/* <p className={styles.formSubtitle}>
                    Enter your details to get started
                  </p> */}

                  <Form onSubmit={handleSubmit}>
                    <Form.Group className={styles.formGroup}>
                      <Form.Label className={styles.formLabel}>
                        Email address
                      </Form.Label>
                      <div className={styles.inputWrapper}>
                        <Mail className={styles.inputIcon} size={18} />
                        <Form.Control
                          type="email"
                          value={email}
                          onChange={(e) => setEmail(e.target.value)}
                          placeholder="name@company.com"
                          className={styles.formInput}
                          required
                        />
                      </div>
                    </Form.Group>

                    <Form.Group className={styles.formGroup}>
                      <Form.Label className={styles.formLabel}>
                        Password
                      </Form.Label>
                      <div className={styles.inputWrapper}>
                        <Lock className={styles.inputIcon} size={18} />
                        <Form.Control
                          type={showPassword ? "text" : "password"}
                          value={password}
                          onChange={(e) => setPassword(e.target.value)}
                          placeholder="Create a password"
                          className={styles.formInput}
                          required
                        />
                        <button
                          type="button"
                          className={styles.passwordToggle}
                          onClick={() => setShowPassword(!showPassword)}
                        >
                          {showPassword ? (
                            <EyeOff size={18} />
                          ) : (
                            <Eye size={18} />
                          )}
                        </button>
                      </div>
                    </Form.Group>

                    <Form.Group className={styles.formGroup}>
                      <Form.Label className={styles.formLabel}>
                        Confirm password
                      </Form.Label>
                      <div className={styles.inputWrapper}>
                        <Lock className={styles.inputIcon} size={18} />
                        <Form.Control
                          type={showConfirmPassword ? "text" : "password"}
                          value={confirmPassword}
                          onChange={(e) => setConfirmPassword(e.target.value)}
                          placeholder="Confirm your password"
                          className={styles.formInput}
                          required
                        />
                        <button
                          type="button"
                          className={styles.passwordToggle}
                          onClick={() =>
                            setShowConfirmPassword(!showConfirmPassword)
                          }
                        >
                          {showConfirmPassword ? (
                            <EyeOff size={18} />
                          ) : (
                            <Eye size={18} />
                          )}
                        </button>
                      </div>
                    </Form.Group>

                    <Form.Check
                      type="checkbox"
                      id="agreeTerms"
                      checked={agreeTerms}
                      onChange={(e) => setAgreeTerms(e.target.checked)}
                      label="I agree to the Terms of Service and Privacy Policy"
                      className={styles.rememberCheck}
                    />

                    {error && (
                      <Alert variant="danger" className="py-2 small">
                        {error}
                      </Alert>
                    )}

                    <Button
                      type="submit"
                      className={styles.submitBtn}
                      disabled={isLoading}
                    >
                      {isLoading ? "Creating account..." : "Create account"}
                      <ArrowRight className="ms-2" size={16} />
                    </Button>
                  </Form>

                  <div className={styles.footer}>
                    Already have an account?{" "}
                    <Button
                      variant="link"
                      className={styles.footerLink}
                      onClick={onBack}
                    >
                      Sign in
                    </Button>
                  </div>
                </Card.Body>
              </Card>
            </Col>
          </Row>
        </Container>
      </div>
    </div>
  );
}
