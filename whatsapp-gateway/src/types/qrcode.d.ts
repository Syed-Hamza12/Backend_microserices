declare module "qrcode" {
    export function toBuffer(text: string, opts?: { type?: "png" }): Promise<Buffer>;
}
