import { env } from "../config/env.js";
import { FirebaseStorageProvider } from "./firebase.storage.provider.js";
import { LocalStorageProvider } from "./local.storage.provider.js";
import type { StorageProvider } from "./storage.provider.js";

function buildProvider(): StorageProvider {
    switch (env.STORAGE_PROVIDER) {
        case "firebase":
            return new FirebaseStorageProvider();
        case "local":
        default:
            return new LocalStorageProvider();
    }
}

/** Single shared instance — providers are stateless wrappers, no reason to rebuild per call. */
export const storageProvider: StorageProvider = buildProvider();

export type { StorageProvider } from "./storage.provider.js";
