import { Fragment, useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import {
  Activity,
  AlertTriangle,
  Archive,
  FolderSearch,
  Gauge,
  LayoutDashboard,
  Settings,
  ShieldAlert,
} from 'lucide-react'
import './App.css'

const API_BASE_URL = 'http://127.0.0.1:8000'
const TERMINAL_STATES = new Set([
  'COMPLETED',
  'COMPLETED_WITH_WARNINGS',
  'FAILED',
  'NO_SUPPORTED_FILES',
  'CANCELLED',
])
const CANONICAL_COMPONENTS = ['DriverWrapper', 'DServer', 'OTX', 'CONXApp', 'ServerAPIConnector']
const EXCLUDED_COMPONENTS = new Set(['CONXApp', 'ServerAPIConnector'])
const SUPPORTED_COMPONENTS = new Set(['DriverWrapper', 'DServer', 'OTX'])

const formatNumber = (value) => Number(value ?? 0).toLocaleString()

const formatBytes = (value) => {
  const numeric = Number(value ?? 0)
  if (numeric === 0) return '0 B'
  if (numeric < 1024) return `${numeric} B`
  if (numeric < 1024 * 1024) return `${(numeric / 1024).toFixed(1)} KB`
  return `${(numeric / (1024 * 1024)).toFixed(1)} MB`
}

function App() {
  const [selectedFile, setSelectedFile] = useState(null)
  const [isUploading, setIsUploading] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  const [result, setResult] = useState(null)
  const [job, setJob] = useState(null)
  const [clusters, setClusters] = useState([])
  const [selectedFingerprint, setSelectedFingerprint] = useState(null)
  const [occurrences, setOccurrences] = useState([])
  const [stage, setStage] = useState('IDLE')
  const [uploadProgress, setUploadProgress] = useState(0)
  const [uploadId, setUploadId] = useState(null)
  const [jobId, setJobId] = useState(null)
  const [jobData, setJobData] = useState(null)
  const [backendStatus, setBackendStatus] = useState('CHECKING_BACKEND')
  const [copiedValue, setCopiedValue] = useState('')
  const [fileSearch, setFileSearch] = useState('')
  const [fileComponentFilter, setFileComponentFilter] = useState('all')
  const [fileStatusFilter, setFileStatusFilter] = useState('all')
  const [fileSortKey, setFileSortKey] = useState('name')
  const [fileSortDirection, setFileSortDirection] = useState('asc')
  const [filePage, setFilePage] = useState(1)
  const [filePageSize, setFilePageSize] = useState(25)
  const [expandedComponents, setExpandedComponents] = useState({
    DriverWrapper: true,
    DServer: true,
    OTX: true,
    CONXApp: true,
    ServerAPIConnector: true,
  })
  const [clusterSearch, setClusterSearch] = useState('')
  const [clusterComponentFilter, setClusterComponentFilter] = useState('all')
  const [clusterSortKey, setClusterSortKey] = useState('occurrences')
  const [clusterSortDirection, setClusterSortDirection] = useState('desc')
  const [clusterPage, setClusterPage] = useState(1)
  const [clusterPageSize, setClusterPageSize] = useState(10)
  const [selectedCluster, setSelectedCluster] = useState(null)
  const [clusterOccurrenceItems, setClusterOccurrenceItems] = useState([])
  const [clusterOccurrenceTotal, setClusterOccurrenceTotal] = useState(0)
  const [clusterOccurrencePage, setClusterOccurrencePage] = useState(1)
  const [clusterOccurrenceLoading, setClusterOccurrenceLoading] = useState(false)
  const [copyMessage, setCopyMessage] = useState('')
  const [expandedOccurrence, setExpandedOccurrence] = useState(null)

  useEffect(() => {
    let isCancelled = false

    async function checkBackend() {
      setStage('CHECKING_BACKEND')
      try {
        await axios.get(`${API_BASE_URL}/health`)
        if (!isCancelled) {
          setBackendStatus('connected')
          setStage((currentStage) => (currentStage === 'CHECKING_BACKEND' || currentStage === 'FAILED' ? 'IDLE' : currentStage))
          setErrorMessage('')
        }
      } catch (healthError) {
        if (!isCancelled) {
          setBackendStatus('disconnected')
          setErrorMessage('Cannot connect to backend. Please start FastAPI on port 8000.')
          setStage('FAILED')
        }
      }
    }

    checkBackend()
    return () => {
      isCancelled = true
    }
  }, [])

  useEffect(() => {
    if (!jobId) {
      return undefined
    }

    const currentStatus = jobData?.status
    if (currentStatus && TERMINAL_STATES.has(currentStatus)) {
      return undefined
    }

    const pollJob = async () => {
      try {
        const response = await axios.get(`${API_BASE_URL}/api/v1/jobs/${jobId}`)
        const nextJob = response.data
        setJob(nextJob)
        setJobData(nextJob)

        if (TERMINAL_STATES.has(nextJob.status)) {
          setStage(nextJob.status)
          if (nextJob.status === 'FAILED' && nextJob.failure_reason) {
            setErrorMessage(nextJob.failure_reason)
          }
          if (nextJob.status === 'COMPLETED' || nextJob.status === 'COMPLETED_WITH_WARNINGS') {
            const clusterResponse = await axios.get(`${API_BASE_URL}/api/v1/jobs/${jobId}/clusters`)
            setClusters(clusterResponse.data.items || [])
          }
          return
        }

        setStage('PROCESSING')
        setErrorMessage('')
      } catch (pollError) {
        setStage('FAILED')
        setErrorMessage(pollError.response?.data?.detail || pollError.message || 'Unable to poll job status.')
      }
    }

    pollJob()
    const timer = setInterval(pollJob, 1000)
    return () => clearInterval(timer)
  }, [jobId, jobData?.status])

  async function handleUpload() {
    console.log('[UPLOAD] Button clicked')

    if (!selectedFile) {
      setStage('FAILED')
      setErrorMessage('Please select a ZIP archive to continue.')
      return
    }

    const fileName = selectedFile.name || ''
    if (!fileName.toLowerCase().endsWith('.zip') && !fileName.toLowerCase().endsWith('.7z')) {
      setStage('FAILED')
      setErrorMessage('Only .zip and .7z uploads are supported.')
      return
    }

    if (backendStatus !== 'connected') {
      setStage('FAILED')
      setErrorMessage('Cannot connect to backend. Please start FastAPI on port 8000.')
      return
    }

    const formData = new FormData()
    formData.append('file', selectedFile)

    try {
      setIsUploading(true)
      setStage('UPLOADING')
      setUploadProgress(0)
      setUploadId(null)
      setJobId(null)
      setJobData(null)
      setJob(null)
      setClusters([])
      setSelectedFingerprint(null)
      setOccurrences([])
      setErrorMessage('')

      const response = await axios.post(
        `${API_BASE_URL}/api/v1/uploads`,
        formData,
        {
          onUploadProgress: (event) => {
            if (event.total) {
              setUploadProgress(Math.round((event.loaded * 100) / event.total))
            }
          },
        },
      )

      console.log('[UPLOAD] Backend response:', response.data)
      const uploadData = response.data
      setResult(uploadData)
      setUploadId(uploadData.upload_id)
      setStage('UPLOAD_RECEIVED')
      setErrorMessage('')

      setStage('CREATING_JOB')
      const jobResponse = await axios.post(`${API_BASE_URL}/api/v1/jobs`, { upload_id: uploadData.upload_id })
      const nextJob = jobResponse.data
      setJob(nextJob)
      setJobData(nextJob)
      setJobId(nextJob.job_id)
      setStage('PROCESSING')
      setErrorMessage('')
    } catch (error) {
      console.error('[UPLOAD] Failed:', error)
      const message = error.response?.data?.detail || error.message || 'Upload failed.'
      setStage('FAILED')
      setErrorMessage(message)
    } finally {
      setIsUploading(false)
    }
  }

  async function loadOccurrences(fingerprintId, page = 1) {
    if (!jobId) return
    try {
      setSelectedFingerprint(fingerprintId)
      setSelectedCluster(null)
      setClusterOccurrencePage(page)
      setClusterOccurrenceLoading(true)
      setExpandedOccurrence(null)
      const response = await axios.get(`${API_BASE_URL}/api/v1/jobs/${jobId}/clusters/${fingerprintId}/occurrences`, {
        params: { page, page_size: 20 },
      })
      setOccurrences(response.data.items || [])
      setClusterOccurrenceItems(response.data.items || [])
      setClusterOccurrenceTotal(response.data.total || 0)
      const cluster = clusters.find((item) => item.fingerprint_id === fingerprintId) || null
      setSelectedCluster(cluster)
    } catch (loadError) {
      setErrorMessage(loadError.response?.data?.detail || loadError.message || 'Unable to load occurrences.')
    } finally {
      setClusterOccurrenceLoading(false)
    }
  }

  const discovery = result?.discovery || null
  const components = discovery?.components || []

  const summaryJob = jobData || job || null
  const totalFiles = summaryJob?.total_files ?? 0
  const processedFiles = summaryJob?.processed_files ?? 0
  const processingProgress = totalFiles > 0 ? Math.round((processedFiles / totalFiles) * 100) : 0

  const backendIndicatorText = backendStatus === 'connected'
    ? 'Backend Connected'
    : backendStatus === 'disconnected'
      ? 'Backend Disconnected'
      : 'Checking Backend...'

  const backendIndicatorClass = backendStatus === 'connected'
    ? 'connected'
    : backendStatus === 'disconnected'
      ? 'disconnected'
      : 'checking'

  const discoveryWarnings = Array.isArray(discovery?.warnings)
    ? discovery.warnings
    : (summaryJob?.warnings ? JSON.parse(summaryJob.warnings) : [])

  const fileRows = useMemo(() => {
    if (!Array.isArray(discovery?.files)) {
      return []
    }

    return discovery.files.map((item) => {
      const component = item.component || 'UNKNOWN'
      let status = 'Unsupported'
      if (EXCLUDED_COMPONENTS.has(component)) {
        status = 'Excluded by scope'
      } else if (SUPPORTED_COMPONENTS.has(component)) {
        status = item.size === 0 && component === 'OTX' ? 'Empty / Inspected' : 'Supported'
      } else if (item.is_log_candidate) {
        status = 'Candidate'
      }

      return {
        ...item,
        component,
        status,
        displayName: item.path?.split('/').pop() || item.path || 'Unknown file',
      }
    })
  }, [discovery])

  const componentBuckets = useMemo(() => {
    const buckets = new Map()
    for (const componentName of CANONICAL_COMPONENTS) {
      buckets.set(componentName, [])
    }

    for (const row of fileRows) {
      const componentName = row.component || 'UNKNOWN'
      if (!buckets.has(componentName)) {
        buckets.set(componentName, [])
      }
      buckets.get(componentName).push(row)
    }

    return [...buckets.entries()].map(([name, rows]) => ({
      name,
      rows,
      count: rows.length,
    }))
  }, [fileRows])

  const visibleFileRows = useMemo(() => {
    const prepared = [...fileRows]

    const query = fileSearch.trim().toLowerCase()
    const filtered = prepared.filter((row) => {
      const matchesSearch = !query || row.path.toLowerCase().includes(query) || row.displayName.toLowerCase().includes(query)
      const matchesComponent = fileComponentFilter === 'all' || row.component === fileComponentFilter
      const matchesStatus = fileStatusFilter === 'all' || row.status === fileStatusFilter
      return matchesSearch && matchesComponent && matchesStatus
    })

    filtered.sort((left, right) => {
      const direction = fileSortDirection === 'asc' ? 1 : -1
      if (fileSortKey === 'size') {
        return (Number(left.size) - Number(right.size)) * direction
      }

      return String(left.displayName).localeCompare(String(right.displayName)) * direction
    })

    const start = (filePage - 1) * filePageSize
    return {
      total: filtered.length,
      pageCount: Math.max(1, Math.ceil(filtered.length / filePageSize)),
      rows: filtered.slice(start, start + filePageSize),
    }
  }, [fileRows, fileSearch, fileComponentFilter, fileStatusFilter, fileSortKey, fileSortDirection, filePage, filePageSize])

  const filteredClusters = useMemo(() => {
    const query = clusterSearch.trim().toLowerCase()
    const filtered = [...clusters].filter((entry) => {
      const matchesQuery = !query || (entry.fingerprint_id || '').toLowerCase().includes(query) || (entry.qualified_function || '').toLowerCase().includes(query)
      const matchesComponent = clusterComponentFilter === 'all' || (entry.components || []).includes(clusterComponentFilter)
      return matchesQuery && matchesComponent
    })

    filtered.sort((left, right) => {
      const direction = clusterSortDirection === 'asc' ? 1 : -1
      if (clusterSortKey === 'occurrences') {
        return (Number(left.occurrence_count) - Number(right.occurrence_count)) * direction
      }
      return String(left.qualified_function).localeCompare(String(right.qualified_function)) * direction
    })

    return filtered
  }, [clusters, clusterSearch, clusterComponentFilter, clusterSortKey, clusterSortDirection])

  const clusterPageCount = Math.max(1, Math.ceil(filteredClusters.length / clusterPageSize))
  const visibleClusters = filteredClusters.slice((clusterPage - 1) * clusterPageSize, clusterPage * clusterPageSize)
  const distribution = useMemo(() => {
    const rows = Array.isArray(discovery?.files) ? discovery.files : []
    const counts = new Map(CANONICAL_COMPONENTS.map((name) => [name, 0]))
    rows.forEach((row) => counts.set(row.component || 'UNKNOWN', (counts.get(row.component || 'UNKNOWN') || 0) + 1))
    return [
      { name: 'DriverWrapper', count: counts.get('DriverWrapper') || 0, color: 'cyan' },
      { name: 'DServer', count: counts.get('DServer') || 0, color: 'blue' },
      { name: 'OTX', count: counts.get('OTX') || 0, color: 'purple' },
      { name: 'Excluded', count: Number(discovery?.excluded_files ?? 0), color: 'slate' },
    ]
  }, [discovery])
  const topClusters = useMemo(() => [...clusters]
    .sort((a, b) => Number(b.occurrence_count || 0) - Number(a.occurrence_count || 0))
    .slice(0, 7), [clusters])

  useEffect(() => {
    setFilePage(1)
  }, [fileSearch, fileComponentFilter, fileStatusFilter, filePageSize])

  useEffect(() => {
    setClusterPage(1)
  }, [clusterSearch, clusterComponentFilter, clusterPageSize])

  useEffect(() => {
    if (!selectedFingerprint) {
      setSelectedCluster(null)
      setClusterOccurrenceItems([])
      setClusterOccurrenceTotal(0)
      return
    }

    if (!jobId) return

    loadOccurrences(selectedFingerprint, clusterOccurrencePage)
  }, [selectedFingerprint, jobId, clusterOccurrencePage])

  const componentFilterOptions = ['all', ...CANONICAL_COMPONENTS]
  const fileStatusOptions = ['all', 'Supported', 'Excluded by scope', 'Candidate', 'Unsupported', 'Empty / Inspected']

  async function handleCopyFingerprint(fingerprintId) {
    try {
      await navigator.clipboard.writeText(fingerprintId)
      setCopyMessage('Fingerprint copied')
    } catch (error) {
      console.error('Copy failed:', error)
      setCopyMessage('Unable to copy fingerprint')
    }
    window.setTimeout(() => setCopyMessage(''), 2200)
  }

  async function copyValue(value, label) {
    if (!value) return
    try {
      await navigator.clipboard.writeText(String(value))
      setCopiedValue(label)
      window.setTimeout(() => setCopiedValue(''), 1500)
    } catch (error) {
      console.error('Failed to copy value:', error)
    }
  }

  const renderStagePanel = () => {
    if (stage === 'IDLE') {
      return (
        <div className="status-panel idle-panel">
          <h3>Ready</h3>
          <p>Select a ZIP or 7z archive and upload it to begin processing.</p>
        </div>
      )
    }

    if (stage === 'CHECKING_BACKEND') {
      return (
        <div className="status-panel checking-panel">
          <h3>Checking Backend</h3>
          <p>Checking backend availability before upload.</p>
        </div>
      )
    }

    if (stage === 'UPLOADING') {
      return (
        <div className="status-panel upload-panel">
          <h3>Uploading to backend</h3>
          <div className="progress-bar"><span style={{ width: `${uploadProgress}%` }} /></div>
          <p>{`Uploading archive to backend: ${uploadProgress}%`}</p>
        </div>
      )
    }

    if (stage === 'UPLOAD_RECEIVED') {
      return (
        <div className="status-panel info-panel">
          <h3>Upload received</h3>
          <p>Upload ID: {uploadId || 'N/A'}</p>
          <p>Archive inspection completed.</p>
        </div>
      )
    }

    if (stage === 'CREATING_JOB') {
      return (
        <div className="status-panel info-panel">
          <h3>Creating job</h3>
          <p>Creating a processing job for the uploaded archive.</p>
        </div>
      )
    }

    if (stage === 'PROCESSING') {
      return (
        <div className="status-panel processing-panel">
          <h3>Processing</h3>
          <p>Job ID: {jobId || 'N/A'}</p>
          <div className="progress-bar"><span style={{ width: `${processingProgress}%` }} /></div>
          <p>{`${processedFiles} of ${totalFiles} files processed (${processingProgress}%)`}</p>
          <div className="metrics-grid">
            <div><label>Records parsed</label><strong>{formatNumber(summaryJob?.parsed_records ?? 0)}</strong></div>
            <div><label>ERROR records</label><strong>{formatNumber(summaryJob?.error_records ?? 0)}</strong></div>
            <div><label>Unique fingerprints</label><strong>{formatNumber(summaryJob?.distinct_fingerprints ?? 0)}</strong></div>
            <div><label>Current file</label><strong className="truncated-value">{summaryJob?.current_file || 'N/A'}</strong></div>
            <div><label>Current component</label><strong>{summaryJob?.current_component || 'N/A'}</strong></div>
          </div>
        </div>
      )
    }

    if (stage === 'COMPLETED' || stage === 'COMPLETED_WITH_WARNINGS') {
      return (
        <div className="status-panel success-panel">
          <h3>{stage === 'COMPLETED' ? 'Completed' : 'Completed with warnings'}</h3>
          <p>{stage === 'COMPLETED' ? 'Processing completed successfully.' : 'Processing completed with warnings.'}</p>
          <div className="metrics-grid">
            <div><label>Files processed</label><strong>{formatNumber(summaryJob?.processed_files ?? 0)}</strong></div>
            <div><label>Records parsed</label><strong>{formatNumber(summaryJob?.parsed_records ?? 0)}</strong></div>
            <div><label>ERROR records</label><strong>{formatNumber(summaryJob?.error_records ?? 0)}</strong></div>
            <div><label>Fingerprint clusters</label><strong>{formatNumber(summaryJob?.distinct_fingerprints ?? 0)}</strong></div>
          </div>
        </div>
      )
    }

    if (stage === 'FAILED' || stage === 'NO_SUPPORTED_FILES') {
      return (
        <div className="status-panel error-panel">
          <h3>{stage}</h3>
          <p>{summaryJob?.failure_reason || errorMessage || 'No supported files were discovered.'}</p>
        </div>
      )
    }

    if (stage === 'CANCELLED') {
      return (
        <div className="status-panel warning-panel">
          <h3>Cancelled</h3>
          <p>The job was cancelled.</p>
        </div>
      )
    }

    return null
  }

  return (
    <div className="dashboard-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Log Analysis Platform</p>
          <h1>Log Processing Dashboard</h1>
        </div>
        <div className="topbar-meta">
          <div className={`backend-indicator ${backendIndicatorClass}`}>
            <span className="backend-dot" />
            {backendIndicatorText}
          </div>
          <div className="job-status-pill">
            <span className="status-label">Status</span>
            <strong>{summaryJob?.status || stage}</strong>
          </div>
        </div>
      </header>

      <main className="dashboard-grid">
        <section className="panel upload-panel">
          <div className="panel-header">
            <div>
              <span className="panel-kicker">Upload</span>
              <h2>Archive intake</h2>
            </div>
          </div>

          <div className="dropzone">
            <input
              id="zip-file"
              type="file"
              accept=".zip,.7z,application/zip,application/x-7z-compressed"
              onChange={(event) => setSelectedFile(event.target.files?.[0] || null)}
            />
            <label htmlFor="zip-file" className="dropzone-label">
              <span className="dropzone-title">{selectedFile ? selectedFile.name : 'Drag & drop ZIP or 7z archive'}</span>
              <span className="dropzone-subtitle">Browse to select an archive</span>
            </label>
          </div>

          <div className="upload-actions">
            <button
              type="button"
              className="primary-button"
              onClick={handleUpload}
              disabled={isUploading || !selectedFile || backendStatus !== 'connected'}
            >
              {isUploading ? 'Uploading...' : 'Upload and Inspect'}
            </button>
            {selectedFile ? (
              <div className="selected-file-meta meta-block">
                <span className="meta-label">Selected file</span>
                <span className="meta-value truncated-value" title={selectedFile.name}>{selectedFile.name}</span>
              </div>
            ) : null}
          </div>

          {uploadProgress > 0 || stage === 'UPLOADING' ? (
            <div className="progress-card">
              <div className="progress-header">
                <span>Upload progress</span>
                <strong>{uploadProgress}%</strong>
              </div>
              <div className="progress-bar"><span style={{ width: `${uploadProgress}%` }} /></div>
            </div>
          ) : null}

          {uploadId || result?.upload_id ? (
            <div className="meta-row">
              <div className="meta-block">
                <span className="meta-label">Upload ID</span>
                <div className="meta-with-action">
                  <span className="meta-value truncated-value" title={uploadId || result?.upload_id}>{uploadId || result?.upload_id || 'N/A'}</span>
                  <button type="button" className="ghost-button" onClick={() => copyValue(uploadId || result?.upload_id, 'Upload ID')}>{copiedValue === 'Upload ID' ? 'Copied' : 'Copy'}</button>
                </div>
              </div>
            </div>
          ) : null}

          {jobId ? (
            <div className="meta-row">
              <div className="meta-block">
                <span className="meta-label">Job ID</span>
                <div className="meta-with-action">
                  <span className="meta-value truncated-value" title={jobId}>{jobId}</span>
                  <button type="button" className="ghost-button" onClick={() => copyValue(jobId, 'Job ID')}>{copiedValue === 'Job ID' ? 'Copied' : 'Copy'}</button>
                </div>
              </div>
            </div>
          ) : null}

          {backendStatus === 'disconnected' ? (
            <div className="status error">Cannot connect to backend. Please start FastAPI on port 8000.</div>
          ) : null}

          {(errorMessage && stage === 'FAILED') ? <div className="status error">{errorMessage}</div> : null}

          {renderStagePanel()}
        </section>

        <aside className="analytics-column">
          <section className="panel analytics-panel">
            <div className="panel-header compact-header"><div><span className="panel-kicker">Discovery</span><h2>Archive component distribution</h2></div></div>
            {discovery ? <div className="distribution-list">{distribution.map((item) => <div className="distribution-row" key={item.name}><div className="distribution-label"><span className={`chart-dot ${item.color}`} />{item.name}<strong>{formatNumber(item.count)}</strong></div><div className="bar-track"><span className={`bar-fill ${item.color}`} style={{ width: `${Math.max(item.count ? 2 : 0, (item.count / Math.max(1, ...distribution.map((part) => part.count))) * 100)}%` }} /></div></div>)}</div> : <div className="empty-state-inline">Upload an archive to see discovered components.</div>}
          </section>
          <section className="panel analytics-panel">
            <div className="panel-header compact-header"><div><span className="panel-kicker">Errors</span><h2>Top error fingerprints</h2></div></div>
            {topClusters.length ? <div className="top-cluster-list">{topClusters.map((cluster, index) => { const max = Math.max(1, ...topClusters.map((item) => Number(item.occurrence_count || 0))); return <div className="top-cluster-row" key={cluster.fingerprint_id}><div className="top-cluster-heading"><span className="rank">{index + 1}</span><span className="top-cluster-function" title={cluster.qualified_function}>{cluster.qualified_function || 'Unknown function'}</span><strong>{formatNumber(cluster.occurrence_count)}</strong></div><div className="bar-track"><span className="bar-fill purple" style={{ width: `${(Number(cluster.occurrence_count || 0) / max) * 100}%` }} /></div><small>{(cluster.components || []).join(', ') || 'UNKNOWN'}</small></div> })}</div> : <div className="empty-state-inline">Error fingerprints appear when job results are ready.</div>}
          </section>
        </aside>

        <section className="panel summary-panel">
          <div className="panel-header compact-header">
            <div>
              <span className="panel-kicker">Overview</span>
              <h2>Processing summary</h2>
            </div>
          </div>

          <div className="metric-grid">
            <div className="metric-card">
              <span>Supported files</span>
              <strong>{formatNumber(discovery?.total_log_files ?? 0)}</strong>
            </div>
            <div className="metric-card">
              <span>Files processed</span>
              <strong>{formatNumber(summaryJob?.processed_files ?? 0)}</strong>
            </div>
            <div className="metric-card">
              <span>Parsed records</span>
              <strong>{formatNumber(summaryJob?.parsed_records ?? 0)}</strong>
            </div>
            <div className="metric-card">
              <span>ERROR records</span>
              <strong>{formatNumber(summaryJob?.error_records ?? 0)}</strong>
            </div>
            <div className="metric-card accent-card">
              <span>Fingerprint clusters</span>
              <strong>{formatNumber(summaryJob?.distinct_fingerprints ?? 0)}</strong>
            </div>
          </div>
        </section>

        <section className="panel discovery-panel">
          <div className="panel-header compact-header">
            <div>
              <span className="panel-kicker">Discovery</span>
              <h2>Archive insights</h2>
            </div>
          </div>

          <div className="discovery-grid">
            <div className="mini-stat">
              <span>Archives</span>
              <strong>{formatNumber(discovery?.archives_discovered ?? 0)}</strong>
            </div>
            <div className="mini-stat">
              <span>Entries</span>
              <strong>{formatNumber(discovery?.total_entries ?? 0)}</strong>
            </div>
            <div className="mini-stat">
              <span>Excluded</span>
              <strong>{formatNumber(discovery?.excluded_files ?? 0)}</strong>
            </div>
            <div className="mini-stat">
              <span>Unsupported</span>
              <strong>{formatNumber(discovery?.unsupported_files ?? 0)}</strong>
            </div>
          </div>

          {result ? (
            <div className="metadata-card">
              <div className="metadata-row"><span>Archive</span><strong className="truncated-value" title={result.filename}>{result.filename}</strong></div>
              <div className="metadata-row"><span>Upload ID</span><strong className="truncated-value" title={uploadId || result.upload_id}>{uploadId || result.upload_id || 'N/A'}</strong></div>
              <div className="metadata-row"><span>Job ID</span><strong className="truncated-value" title={jobId}>{jobId || 'N/A'}</strong></div>
            </div>
          ) : null}
        </section>

        {discovery?.warnings?.length ? (
          <section className="panel warnings-panel">
            <div className="panel-header compact-header">
              <div>
                <span className="panel-kicker">Notices</span>
                <h2>Warnings</h2>
              </div>
            </div>
            <ul className="warning-list">
              {discoveryWarnings.map((warning, index) => (
                <li key={`${warning}-${index}`}>{warning}</li>
              ))}
            </ul>
          </section>
        ) : null}

        {fileRows.length > 0 ? (
          <section className="panel explorer-panel">
            <div className="panel-header explorer-header">
              <div>
                <span className="panel-kicker">File Explorer</span>
                <h2>Archive entries</h2>
              </div>
            </div>

            <div className="filters-row">
              <input
                type="search"
                value={fileSearch}
                onChange={(event) => setFileSearch(event.target.value)}
                placeholder="Search filename or path"
                aria-label="Search file names"
              />
              <select value={fileComponentFilter} onChange={(event) => setFileComponentFilter(event.target.value)}>
                {componentFilterOptions.map((option) => (
                  <option key={option} value={option}>{option === 'all' ? 'All components' : option}</option>
                ))}
              </select>
              <select value={fileStatusFilter} onChange={(event) => setFileStatusFilter(event.target.value)}>
                {fileStatusOptions.map((option) => (
                  <option key={option} value={option}>{option === 'all' ? 'All statuses' : option}</option>
                ))}
              </select>
              <select value={filePageSize} onChange={(event) => { setFilePageSize(Number(event.target.value)); setFilePage(1); }}>
                <option value={25}>25 per page</option>
                <option value={50}>50 per page</option>
              </select>
            </div>

            <div className="component-sections">
              {componentBuckets.map((bucket) => {
                const visibleRows = bucket.rows.filter((row) => {
                  const searchMatch = !fileSearch || row.path.toLowerCase().includes(fileSearch.toLowerCase()) || row.displayName.toLowerCase().includes(fileSearch.toLowerCase())
                  const componentMatch = fileComponentFilter === 'all' || row.component === fileComponentFilter
                  const statusMatch = fileStatusFilter === 'all' || row.status === fileStatusFilter
                  return searchMatch && componentMatch && statusMatch
                })

                if (!visibleRows.length && fileComponentFilter !== 'all' && fileComponentFilter !== bucket.name) {
                  return null
                }

                if (!visibleRows.length && fileComponentFilter === 'all' && !bucket.count) {
                  return null
                }

                const isExpanded = expandedComponents[bucket.name] ?? true
                const pagedRows = visibleRows.slice((filePage - 1) * filePageSize, filePage * filePageSize)

                return (
                  <div key={bucket.name} className="component-section">
                    <button type="button" className="component-toggle" onClick={() => setExpandedComponents((current) => ({ ...current, [bucket.name]: !isExpanded }))}>
                      <span>{bucket.name}</span>
                      <strong>{bucket.count}</strong>
                    </button>

                    {isExpanded ? (
                      <div className="component-body">
                        {pagedRows.length ? (
                          <div className="table-wrap">
                            <table>
                              <thead>
                                <tr>
                                  <th>File name</th>
                                  <th>Size</th>
                                  <th>Status</th>
                                  <th>Actions</th>
                                </tr>
                              </thead>
                              <tbody>
                                {pagedRows.map((row) => (
                                  <tr key={`${row.component}-${row.path}`}>
                                    <td className="path-cell" title={row.path}>
                                      <span className="cell-text">{row.displayName}</span>
                                      <small className="full-path">{row.path}</small>
                                    </td>
                                    <td>{formatBytes(row.size)}</td>
                                    <td><span className={`status-badge ${row.status === 'Excluded by scope' ? 'excluded' : row.status === 'Supported' || row.status === 'Empty / Inspected' ? 'supported' : 'other'}`}>{row.status}</span></td>
                                    <td>
                                      <button type="button" className="ghost-button compact" onClick={() => copyValue(row.path, 'File path')}>Copy path</button>
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        ) : (
                          <div className="empty-state-inline">No matching files in this component.</div>
                        )}
                      </div>
                    ) : null}
                  </div>
                )
              })}
            </div>

            {visibleFileRows.total > filePageSize ? (
              <div className="pager">
                <button type="button" disabled={filePage === 1} onClick={() => setFilePage((page) => Math.max(1, page - 1))}>Previous</button>
                <span>Page {filePage} of {visibleFileRows.pageCount}</span>
                <button type="button" disabled={filePage >= visibleFileRows.pageCount} onClick={() => setFilePage((page) => Math.min(visibleFileRows.pageCount, page + 1))}>Next</button>
              </div>
            ) : null}
          </section>
        ) : null}

        {clusters.length > 0 ? (
          <section className="panel cluster-panel">
            <div className="panel-header compact-header">
              <div>
                <span className="panel-kicker">Error Explorer</span>
                <h2>Fingerprint clusters</h2>
              </div>
            </div>

            <div className="filters-row">
              <input
                type="search"
                value={clusterSearch}
                onChange={(event) => setClusterSearch(event.target.value)}
                placeholder="Search fingerprint or function"
                aria-label="Search clusters"
              />
              <select value={clusterComponentFilter} onChange={(event) => setClusterComponentFilter(event.target.value)}>
                <option value="all">All components</option>
                {CANONICAL_COMPONENTS.map((component) => (
                  <option key={component} value={component}>{component}</option>
                ))}
              </select>
              <select value={clusterSortKey} onChange={(event) => setClusterSortKey(event.target.value)}>
                <option value="occurrences">Sort by occurrences</option>
                <option value="function">Sort by function</option>
              </select>
              <select value={clusterSortDirection} onChange={(event) => setClusterSortDirection(event.target.value)}>
                <option value="desc">Descending</option>
                <option value="asc">Ascending</option>
              </select>
              <select value={clusterPageSize} onChange={(event) => { setClusterPageSize(Number(event.target.value)); setClusterPage(1); }}>
                <option value={10}>10 per page</option>
                <option value={25}>25 per page</option>
              </select>
            </div>

            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Fingerprint</th>
                    <th>Qualified function</th>
                    <th>Component</th>
                    <th>Occurrences</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleClusters.map((cluster) => (
                    <tr key={cluster.fingerprint_id}>
                      <td className="truncated-value" title={cluster.fingerprint_id}>{cluster.fingerprint_id}</td>
                      <td className="truncated-value" title={cluster.qualified_function}>{cluster.qualified_function}</td>
                      <td>{(cluster.components || []).join(', ') || 'UNKNOWN'}</td>
                      <td>{formatNumber(cluster.occurrence_count)} occurrences</td>
                      <td>
                        <div className="inline-actions">
                          <button type="button" className="ghost-button compact" onClick={(event) => { event.stopPropagation(); handleCopyFingerprint(cluster.fingerprint_id) }}>Copy</button>
                          <button type="button" className="primary-button compact" onClick={(event) => { event.stopPropagation(); loadOccurrences(cluster.fingerprint_id, 1) }}>View</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {clusterPageCount > 1 ? (
              <div className="pager">
                <button type="button" disabled={clusterPage === 1} onClick={() => setClusterPage((page) => Math.max(1, page - 1))}>Previous</button>
                <span>Page {clusterPage} of {clusterPageCount}</span>
                <button type="button" disabled={clusterPage >= clusterPageCount} onClick={() => setClusterPage((page) => Math.min(clusterPageCount, page + 1))}>Next</button>
              </div>
            ) : null}
          </section>
        ) : null}

        {copyMessage ? <div className={`copy-toast ${copyMessage.startsWith('Unable') ? 'error' : ''}`} role="status">{copyMessage}</div> : null}

        {selectedCluster || selectedFingerprint ? (
          <section className="panel detail-panel">
            <div className="panel-header compact-header">
              <div>
                <span className="panel-kicker">Details</span>
                <h2>Occurrence detail</h2>
              </div>
            </div>

            <div className="detail-grid">
              <div className="detail-card">
                <span>Fingerprint</span>
                <strong>{selectedCluster?.fingerprint_id || selectedFingerprint}</strong>
              </div>
              <div className="detail-card"><span>Normalized message</span><strong className="detail-message">{selectedCluster?.normalized_message || 'N/A'}</strong></div>
              <div className="detail-card"><span>Component</span><strong>{(selectedCluster?.components || []).join(', ') || clusterOccurrenceItems[0]?.component || 'UNKNOWN'}</strong></div>
              <div className="detail-card">
                <span>Qualified function</span>
                <strong className="truncated-value" title={selectedCluster?.qualified_function || 'N/A'}>{selectedCluster?.qualified_function || 'N/A'}</strong>
              </div>
              <div className="detail-card">
                <span>Total occurrences</span>
                <strong>{formatNumber(selectedCluster?.occurrence_count || clusterOccurrenceTotal || 0)}</strong>
              </div>
            </div>

            {clusterOccurrenceLoading ? <div className="loading-row">Loading occurrences…</div> : null}

            <div className="occurrence-list">
              {clusterOccurrenceItems.length === 0 ? (
                <div className="empty-state-inline">No occurrences available.</div>
              ) : (
                <div className="table-wrap occurrence-table-wrap"><table><thead><tr><th>File</th><th>Component</th><th>Source line</th><th>Timestamp</th><th>Thread</th><th>Details</th></tr></thead><tbody>{clusterOccurrenceItems.map((entry, index) => (
                  <Fragment key={`${selectedFingerprint}-${index}`}>
                    <tr><td title={entry.source_file} className="occurrence-path">{entry.source_file || 'N/A'}</td><td>{entry.component || 'UNKNOWN'}</td><td>{entry.source_line ?? 'N/A'}</td><td>{entry.timestamp || 'N/A'}</td><td>{entry.thread_id || 'N/A'}</td><td><button type="button" className="ghost-button compact" onClick={() => setExpandedOccurrence(expandedOccurrence === index ? null : index)}>{expandedOccurrence === index ? 'Hide' : 'Raw log'}</button></td></tr>
                    {expandedOccurrence === index ? <tr><td colSpan="6"><div className="raw-details"><strong>Archive path</strong><p>{entry.source_file || 'N/A'}</p><strong>Raw log line</strong><pre>{entry.raw_message || entry.message || 'No raw log line available.'}</pre></div></td></tr> : null}
                  </Fragment>
                ))}</tbody></table></div>
              )}
            </div>

            {clusterOccurrenceTotal > 20 ? (
              <div className="pager">
                <button type="button" disabled={clusterOccurrencePage === 1} onClick={() => setClusterOccurrencePage((page) => Math.max(1, page - 1))}>Previous</button>
                <span>Page {clusterOccurrencePage} of {Math.max(1, Math.ceil(clusterOccurrenceTotal / 20))}</span>
                <button type="button" disabled={clusterOccurrencePage >= Math.ceil(clusterOccurrenceTotal / 20)} onClick={() => setClusterOccurrencePage((page) => page + 1)}>Next</button>
              </div>
            ) : null}
          </section>
        ) : null}
      </main>
    </div>
  )
}

export default App
