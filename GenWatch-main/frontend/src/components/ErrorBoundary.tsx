// Keeps a render error inside the view that caused it.
//
// React unmounts the entire tree when a render throws, so one bad field in
// one panel took the whole console to a black screen — topbar, comms
// badge, nav and all. On an operator console that is the worst possible
// failure mode: the moment you most need to see comms state is an outage,
// and "blank" is indistinguishable from "the server is gone".
//
// Wrapping each view means a crash costs you that view and nothing else.
// The topbar keeps showing comms health, and the other tabs still work.

import { Component, type ErrorInfo, type ReactNode } from "react";
import { Icon } from "./primitives";

interface Props {
  children: ReactNode;
  /** Shown in the message, e.g. "Settings". */
  label?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Keep the stack in the console — this is the only record, since the
    // UI deliberately shows the operator a short message rather than a
    // component trace.
    console.error("view render failed:", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const what = this.props.label ? `The ${this.props.label} view` : "This view";
    return (
      <div className="settings-section" role="alert">
        <div className="settings-head">
          <h2>
            <Icon name="x" size={14} /> {what} failed to render
          </h2>
          <p>
            The rest of the console is still live — switch tabs to keep working, and check the
            comms badge above for the state of the generator link.
          </p>
          <p>
            This most often means the browser is running a newer interface than the service
            behind it, which happens between installing an update and restarting it:
            <br />
            <span className="mono">sudo systemctl restart genwatch</span>
            <br />
            then reload the page. If it persists after a restart, the details below are worth
            reporting.
          </p>
        </div>
        <div className="field-row">
          <div className="lbl">
            Error
            <span className="desc mono">{error.message || String(error)}</span>
          </div>
          <button className="btn" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}
