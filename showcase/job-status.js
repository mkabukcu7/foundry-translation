"use strict";

async function fetchJobStatus(url, onReconnect) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await fetch(url);
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
      if (attempt === 5) {
        throw new Error("Cannot reach the local showcase to check job status. The job may still be running; check Saved Demonstrations before submitting again.");
      }
      onReconnect();
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
  }
}
