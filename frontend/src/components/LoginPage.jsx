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
import { loginUser } from "../api/client.js";
import { toast } from "react-toastify";
import { LoadingSpinner } from "./LoadingSpinner.jsx";

const FEATURES = [
  {
    icon: FileText,
    title: "Understand business requirements",
    description: "Extract user stories, rules and entities",
  },
  {
    icon: ChefHat,
    title: "Build data recipes",
    description: "Map to the data model",
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
          validated synthetic data for your data integration use cases.
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

export function LoginPage({ onLogin, onRegister }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [rememberMe, setRememberMe] = useState(false);
  const [error, setError] = useState(null);
  const [isLoading, setIsLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);

    if (!email || !password) {
      setError("Please fill in all fields");
      return;
    }

    setIsLoading(true);
    try {
      const user = await loginUser(email, password);
      localStorage.setItem("testiq_current_user", JSON.stringify(user));
      localStorage.setItem("testiq_local_session", "true");
      toast.success("Login successful. Welcome back to TestIQ.");
      onLogin(user);
      return;
    } catch {
      // Keep the local POC account fallback for offline development.
    }
    setTimeout(() => {
      const users = JSON.parse(localStorage.getItem("testiq_users") || "{}");
      const user = users[email];

      if (!user || user.password !== password) {
        setError("Invalid email or password");
        setIsLoading(false);
        return;
      }

      localStorage.setItem("testiq_current_user", JSON.stringify(user));
      localStorage.setItem("testiq_local_session", "true");
      toast.success("Login successful. Welcome back to TestIQ.");
      onLogin(user);
      setIsLoading(false);
    }, 500);
  }

  return (
    <div className={styles.page}>
      <BrandHeader />

      <div className={styles.main}>
        <Container fluid className={styles.container}>
          <Row className="g-4 align-items-top">
            <Col lg={7}>
              <ProductPanel />
            </Col>

            <Col lg={5}>
              <Card className={styles.formCard}>
                <Card.Body className={styles.formCardBody}>
                  <h3 className={styles.formTitle}>Welcome back</h3>
                  <p className={styles.formSubtitle}>
                    Sign in to continue to TestIQ
                  </p>

                  {error && (
                    <Alert
                      variant="danger"
                      className="mb-4"
                      style={{ fontSize: "0.9rem" }}
                    >
                      {error}
                    </Alert>
                  )}

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
                          placeholder="Enter your password"
                          className={styles.formInput}
                          required
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword(!showPassword)}
                          className={styles.passwordToggle}
                        >
                          {showPassword ? (
                            <EyeOff size={18} />
                          ) : (
                            <Eye size={18} />
                          )}
                        </button>
                      </div>
                    </Form.Group>

                    <div className={styles.formExtras}>
                      <Form.Check
                        type="checkbox"
                        id="rememberMe"
                        checked={rememberMe}
                        onChange={(e) => setRememberMe(e.target.checked)}
                        label="Remember me"
                        className={styles.rememberCheck}
                      />
                      <a href="#forgot" className={styles.forgotLink}>
                        Forgot password?
                      </a>
                    </div>

                    <Button
                      type="submit"
                      className={styles.submitBtn}
                      disabled={isLoading}
                    >
                      {isLoading ? <LoadingSpinner variant="inline" label="Signing in" /> : <>Sign in <ArrowRight size={16} className="ms-2" /></>}
                    </Button>
                  </Form>
                  <p className={styles.footer}>
                    Don't have an account?{" "}
                    <button onClick={onRegister} className={styles.footerLink}>
                      Register
                    </button>
                  </p>
                </Card.Body>
              </Card>
            </Col>
          </Row>
        </Container>
      </div>
    </div>
  );
}
