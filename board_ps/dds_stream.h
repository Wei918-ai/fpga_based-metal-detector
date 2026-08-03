#ifndef DDS_STREAM_H
#define DDS_STREAM_H

/* Call once after network initialization is complete (in main.c after board IP is printed) */
int dds_stream_init(void);

/* Call repeatedly in the main loop of main.c (placed after xemacif_input()) */
void dds_stream_poll(void);

#endif
