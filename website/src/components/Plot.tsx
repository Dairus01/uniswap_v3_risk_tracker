'use client';
import dynamic from 'next/dynamic';
import React from 'react';

const Plot = dynamic(() => import('react-plotly.js'), { ssr: false, loading: () => <div>Loading chart...</div> });

export default Plot as unknown as React.ComponentType<any>;


