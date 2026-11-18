"use strict";

const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("arcreelDesktop", {
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
});
