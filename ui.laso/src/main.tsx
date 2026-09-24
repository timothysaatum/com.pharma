import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./App.css";
import { probeBackendNow, startBackendHeartbeat } from "@/api/client";

// Probe the backend BEFORE the first render so every offline guard
// (`isBackendKnownUnreachable`, `!navigator.onLine`) has an accurate
// value when pages mount. Without this, `backendReachable` defaults to
// `true` and all pages optimistically hit the API before it has had a
// chance to fail and flip the flag.
// We AWAIT the probe to eliminate the cold-start race where pages mount
// before `backendReachable` is correctly set (up to 5s delay otherwise).
(async () => {
  await probeBackendNow();
  startBackendHeartbeat();

  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
})();