# AVS3 Audio decoder sources

Reference workflow: https://github.com/nilaoda/av3a_decoder (aaf2108a5b4e73fe95687ae88687e554dff829c7).
Native source mirror: https://github.com/zlxyc/Sourcecodeforplayer
Pinned revision: e7d244d29454eb04c968cd98a30587303a9c15f8. Only decoder/common source files required by the macOS helper are included.
The upstream command-line main, Android emulator, proprietary logging header, renderer and prebuilt binaries are excluded.

Source files retain their original notices, including the UHD World Association Code Sharing Policy reference:
http://www.theuwa.com/UWA_Code_Sharing_Policy.pdf
No additional license or official Audio Vivid certification is implied by this integration.

Local portability changes: none. The helper validates supported headers and CRCs before calling the decoder, zero-initializes its state, and supplies its own TS demux, PCM stream and WAV writer.
