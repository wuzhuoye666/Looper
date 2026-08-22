package main

import (
	"encoding/json"
	"encoding/xml"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
)

const sourceRevision = "33c00473807ebd49304d114a6d769d2d2b2bbb34"

type envelope struct {
	Target struct {
		Inventory map[string]any `json:"inventory"`
	} `json:"target"`
	Extensions struct {
		OfferedLoad float64 `json:"offeredLoad"`
	} `json:"extensions"`
}

type databaseSecret struct {
	Username           string  `json:"username"`
	Password           string  `json:"password"`
	Database           string  `json:"database"`
	Port               int     `json:"port"`
	SSLMode            string  `json:"sslMode"`
	ScaleFactor        float64 `json:"scaleFactor"`
	Terminals          int     `json:"terminals"`
	MeasurementSeconds int     `json:"measurementSeconds"`
	CreateSchema       bool    `json:"createSchema"`
	LoadData           bool    `json:"loadData"`
}

type summaryDocument struct {
	ElapsedNanoseconds int64 `json:"Elapsed Time (nanoseconds)"`
	MeasuredRequests   int64 `json:"Measured Requests"`
}

type clientAccounting struct {
	SchemaVersion        string  `json:"schemaVersion"`
	PlannedOfferedTPS    float64 `json:"plannedOfferedTps"`
	MeasurementSeconds   float64 `json:"measurementSeconds"`
	OfferedRequests      int64   `json:"offeredRequests"`
	StartedRequests      int64   `json:"startedRequests"`
	CompletedRequests    int64   `json:"completedRequests"`
	TimeoutRequests      int64   `json:"timeoutRequests"`
	RateLimiterLagRatio  float64 `json:"rateLimiterLagRatio"`
	ClientHeadroomRatio  float64 `json:"clientHeadroomRatio"`
}

func main() {
	envelopePath := flag.String("envelope", "", "Looper run envelope")
	outputPath := flag.String("output", "", "Looper output directory")
	selfCheck := flag.Bool("self-check", false, "validate the packaged runtime")
	flag.Parse()
	if *selfCheck {
		if err := checkRuntime(); err != nil {
			fatal(err)
		}
		fmt.Printf("source_revision=%s\nruntime=ready\n", sourceRevision)
		return
	}
	if *envelopePath == "" || *outputPath == "" {
		fatal(errors.New("--envelope and --output are required"))
	}
	if err := run(*envelopePath, *outputPath); err != nil {
		fatal(err)
	}
}

func checkRuntime() error {
	if _, err := os.Stat("/opt/benchbase/benchbase.jar"); err != nil {
		return fmt.Errorf("BenchBase jar is missing: %w", err)
	}
	command := exec.Command("java", "-version")
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	return command.Run()
}

func run(envelopePath, outputPath string) error {
	var runEnvelope envelope
	if err := readJSON(envelopePath, &runEnvelope); err != nil {
		return fmt.Errorf("read run envelope: %w", err)
	}
	if !isFinitePositive(runEnvelope.Extensions.OfferedLoad) {
		return errors.New("run envelope has no positive offeredLoad")
	}
	secret := databaseSecret{
		Database: "benchbase", Port: 5432, SSLMode: "disable", ScaleFactor: 10,
		Terminals: 256, MeasurementSeconds: 600,
	}
	if err := readJSON("/run/looper-secrets/postgres.json", &secret); err != nil {
		return fmt.Errorf("read worker-local PostgreSQL secret: %w", err)
	}
	if secret.Username == "" || secret.Database == "" || secret.Port < 1 {
		return errors.New("PostgreSQL secret is incomplete")
	}
	host := inventoryString(runEnvelope.Target.Inventory, "private_ip", "privateIp", "endpoint")
	if host == "" {
		return errors.New("target inventory has no private PostgreSQL endpoint")
	}
	if secret.MeasurementSeconds < 1 || secret.Terminals < 1 || secret.ScaleFactor <= 0 {
		return errors.New("PostgreSQL workload settings are invalid")
	}
	if err := os.MkdirAll(outputPath, 0o755); err != nil {
		return err
	}
	rawDirectory := filepath.Join(outputPath, "benchbase-raw")
	if err := os.MkdirAll(rawDirectory, 0o755); err != nil {
		return err
	}
	configurationPath := filepath.Join(os.TempDir(), "smallbank.xml")
	configuration := renderConfiguration(host, secret, runEnvelope.Extensions.OfferedLoad)
	if err := os.WriteFile(configurationPath, []byte(configuration), 0o600); err != nil {
		return err
	}
	defer os.Remove(configurationPath)

	arguments := []string{
		"-jar", "/opt/benchbase/benchbase.jar", "-b", "smallbank", "-c", configurationPath,
		fmt.Sprintf("--create=%t", secret.CreateSchema),
		fmt.Sprintf("--load=%t", secret.LoadData),
		"--execute=true", "--sample=5", "--directory", rawDirectory,
		"--json-histograms", filepath.Join(outputPath, "transaction-histograms.json"),
	}
	logPath := filepath.Join(outputPath, "upstream.log")
	logFile, err := os.Create(logPath)
	if err != nil {
		return err
	}
	defer logFile.Close()
	command := exec.Command("java", arguments...)
	command.Dir = "/opt/benchbase"
	command.Stdout = io.MultiWriter(os.Stdout, logFile)
	command.Stderr = io.MultiWriter(os.Stderr, logFile)
	started := time.Now()
	err = command.Run()
	wallSeconds := time.Since(started).Seconds()
	if err != nil {
		return fmt.Errorf("BenchBase exited unsuccessfully: %w", err)
	}

	summaryPath, err := onlyMatch(filepath.Join(rawDirectory, "*.summary.json"))
	if err != nil {
		return err
	}
	rawLatencyPath, err := onlyMatch(filepath.Join(rawDirectory, "*.raw.csv"))
	if err != nil {
		return err
	}
	if err := copyFile(summaryPath, filepath.Join(outputPath, "summary.json")); err != nil {
		return err
	}
	if err := copyFile(rawLatencyPath, filepath.Join(outputPath, "latency.raw.csv")); err != nil {
		return err
	}
	var summary summaryDocument
	if err := readJSON(summaryPath, &summary); err != nil {
		return fmt.Errorf("read BenchBase summary: %w", err)
	}
	measurementSeconds := float64(summary.ElapsedNanoseconds) / 1_000_000_000
	if measurementSeconds <= 0 || summary.MeasuredRequests < 1 {
		return errors.New("BenchBase summary has no measurement evidence")
	}
	plannedRequests := int64(math.Round(runEnvelope.Extensions.OfferedLoad * measurementSeconds))
	offeredRequests := max(plannedRequests, summary.MeasuredRequests)
	lagRatio := math.Max(0, 1-float64(summary.MeasuredRequests)/float64(max(plannedRequests, 1)))
	cpuSeconds := 0.0
	if command.ProcessState != nil {
		cpuSeconds = command.ProcessState.UserTime().Seconds() + command.ProcessState.SystemTime().Seconds()
	}
	clientUtilization := cpuSeconds / math.Max(wallSeconds*float64(runtime.NumCPU()), 0.001)
	headroom := math.Max(0, math.Min(1, 1-clientUtilization))
	accounting := clientAccounting{
		SchemaVersion: "v1alpha1", PlannedOfferedTPS: runEnvelope.Extensions.OfferedLoad,
		MeasurementSeconds: measurementSeconds, OfferedRequests: offeredRequests,
		StartedRequests: summary.MeasuredRequests, CompletedRequests: summary.MeasuredRequests,
		TimeoutRequests: 0, RateLimiterLagRatio: lagRatio, ClientHeadroomRatio: headroom,
	}
	if err := writeJSON(filepath.Join(outputPath, "client-load-accounting.json"), accounting); err != nil {
		return err
	}
	return nil
}

func renderConfiguration(host string, secret databaseSecret, offeredLoad float64) string {
	url := fmt.Sprintf(
		"jdbc:postgresql://%s:%d/%s?sslmode=%s&ApplicationName=smallbank&reWriteBatchedInserts=true",
		host, secret.Port, secret.Database, secret.SSLMode,
	)
	return fmt.Sprintf(`<?xml version="1.0"?>
<parameters>
  <type>POSTGRES</type><driver>org.postgresql.Driver</driver>
  <url>%s</url><username>%s</username><password>%s</password>
  <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
  <isolation>TRANSACTION_SERIALIZABLE</isolation><batchsize>128</batchsize>
  <scalefactor>%s</scalefactor><terminals>%d</terminals>
  <works><work><time>%d</time><rate>%s</rate><weights>15,15,15,25,15,15</weights></work></works>
  <transactiontypes>
    <transactiontype><name>Amalgamate</name></transactiontype>
    <transactiontype><name>Balance</name></transactiontype>
    <transactiontype><name>DepositChecking</name></transactiontype>
    <transactiontype><name>SendPayment</name></transactiontype>
    <transactiontype><name>TransactSavings</name></transactiontype>
    <transactiontype><name>WriteCheck</name></transactiontype>
  </transactiontypes>
</parameters>
`, xmlEscape(url), xmlEscape(secret.Username), xmlEscape(secret.Password),
		strconv.FormatFloat(secret.ScaleFactor, 'f', -1, 64), secret.Terminals,
		secret.MeasurementSeconds, strconv.FormatFloat(offeredLoad, 'f', -1, 64))
}

func xmlEscape(value string) string {
	var builder strings.Builder
	_ = xml.EscapeText(&builder, []byte(value))
	return builder.String()
}

func inventoryString(inventory map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := inventory[key].(string); ok && strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func onlyMatch(pattern string) (string, error) {
	matches, err := filepath.Glob(pattern)
	if err != nil {
		return "", err
	}
	sort.Strings(matches)
	if len(matches) != 1 {
		return "", fmt.Errorf("expected one file matching %s, found %d", pattern, len(matches))
	}
	return matches[0], nil
}

func copyFile(source, destination string) error {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.Create(destination)
	if err != nil {
		return err
	}
	if _, err := io.Copy(output, input); err != nil {
		output.Close()
		return err
	}
	return output.Close()
}

func readJSON(path string, destination any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, destination)
}

func writeJSON(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return os.WriteFile(path, data, 0o644)
}

func isFinitePositive(value float64) bool {
	return value > 0 && !math.IsInf(value, 0) && !math.IsNaN(value)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(2)
}
