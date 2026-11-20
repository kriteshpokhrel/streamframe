export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Word {
  start: number;
  end: number;
  text: string;
  correction?: string;
  [key: string]: unknown;
}

export interface Candidate {
  id: string;
  start: number;
  end: number;
  score: number;
  evidence: unknown;
  words: Word[];
  face_crop?: Rect | null;
  content_crop?: Rect | null;
  editable?: boolean;
  media_url?: string | null;
  source_media_url?: string | null;
  window_index?: number;
  window_duration?: number;
  revision?: number;
  export_status?: string | null;
  export_error?: string | null;
  [key: string]: unknown;
}

interface RollingOptions {
  rolling?: boolean;
  session_duration?: number;
  window_duration?: number;
}

export interface JobExport {
  candidate_id: string;
  filename: string;
  url?: string;
  window_start?: number;
  window_end?: number;
  global_start?: number;
  global_end?: number;
  revision?: number;
  window_index?: number;
  created_at?: string;
  start?: number;
  end?: number;
}

export interface Job {
  id: string;
  status: string;
  progress: number;
  candidates: Candidate[];
  error?: unknown;
  source: string | { type: 'file'; path: string; start?: number; end?: number } |
    ({ type: 'url'; url: string; start?: number; end?: number;
      mode?: 'auto' | 'live' | 'latest'; duration?: number } & RollingOptions) |
    ({ type: 'streamer'; platform: 'twitch' | 'youtube'; identifier: string;
      mode: 'auto' | 'live' | 'latest'; duration?: number; start?: number; end?: number } & RollingOptions);
  exports?: JobExport[];
  live_session?: {
    phase: string;
    windows_seen: number;
    windows_processed: number;
    global_time: number;
    warnings?: string[] | string;
    face_crop?: Rect | null;
    windows?: { index: number; status: string; start?: number; end?: number; warning?: string }[];
  } | null;
  resolved_source?: { url: string; platform: string; title: string; is_live: boolean; selection: string } | null;
  capture_started_at?: string | null;
  [key: string]: unknown;
}

export type CandidateChanges = Partial<Pick<Candidate,
  'start' | 'end' | 'words' | 'face_crop' | 'content_crop'
>>;

export interface ExportFile {
  name: string;
  url: string;
}

export interface ExportedVideo extends ExportFile {
  jobId: string;
  candidateId?: string;
  provenance?: JobExport;
}
