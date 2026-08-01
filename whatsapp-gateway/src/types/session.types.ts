export enum SessionStatus {
    CREATED = "CREATED",
    CONNECTING = "CONNECTING",
    QR_READY = "QR_READY",
    CONNECTED = "CONNECTED",
    DISCONNECTED = "DISCONNECTED",
    RECONNECTING = "RECONNECTING",
    UNLINKED = "UNLINKED",
    ERROR = "ERROR"
}

export interface GatewaySession {
    id: string;
    displayName: string;
    status: SessionStatus;
    phone?: string;
    qr?: string;
    createdAt: Date;
    updatedAt: Date;
    connectedAt?: Date;
    /** Consecutive automatic reconnects since the last successful open. Reset on connect. */
    reconnectAttempts: number;
    /** QR codes handed out during the current connect attempt — capped by QR_MAX_CYCLES. */
    qrCycles: number;
    /** Why the session last stopped, surfaced to Django/the app instead of a bare ERROR. */
    lastError?: string;
    /** Set when the session gave up and needs an explicit human-triggered reconnect. */
    needsManualReconnect?: boolean;
}
