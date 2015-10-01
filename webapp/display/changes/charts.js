import React, { PropTypes } from 'react';

import ChangesLinks from 'es6!display/changes/links';
import SimpleTooltip from 'es6!display/simple_tooltip';
import { ProgrammingError } from 'es6!display/errors';
import { get_runnable_condition, get_runnable_condition_color_cls } from 'es6!display/changes/build_conditions';

/*
 * Renders a small bar chart of a series of builds/tests/maybe others
 */
export var ChangesChart = React.createClass({

  MAX_CHART_HEIGHT: 40,  // pixels
  
  propTypes: {
    type: PropTypes.oneOf(['build', 'test']).isRequired,
    runnables: PropTypes.array.isRequired,
    leftEllipsis: PropTypes.bool,
    rightEllipsis: PropTypes.bool,
    // if enabled, we'll show a 'latest' label when leftEllipsis = false
    enableLatest: PropTypes.bool
  },

  getDefaultProps() {
    return { leftEllipsis: false, rightEllipsis: false, enableLatest: true };
  },
  
  render() {
    var runnables = this.props.runnables;
    
    // we'll render bar heights relative to this
    var longestDuration = _.max(runnables, r => r && r.duration).duration;

    var content = _.map(this.props.runnables, runnable => {
      var no_duration = !runnable.duration && runnable.duration === 0;
      if (_.isEmpty(runnable) || no_duration) {
        // would be nice to still show a tooltip here...
        return <div 
          className="chartBarColumn"
          style={{ paddingTop: this.MAX_CHART_HEIGHT - 2 }}>
          <div
            className="emptyChartBar"
            style={{height: 2}}
          />
        </div>;
      }

      var heightPercentage = runnable.duration / longestDuration;
      var barHeight = Math.floor(heightPercentage * this.MAX_CHART_HEIGHT) || 1;

      var columnPadding = this.MAX_CHART_HEIGHT - barHeight;

      var bgColor = get_runnable_condition_color_cls(
        get_runnable_condition(runnable),
        true);

      var tooltipText = null, href = null;
      if (this.props.type === 'build') {
        // TODO: show more details about the build
        tooltipText = runnable.name;
        href = ChangesLinks.buildHref(runnable);
      } else if (this.props.type === 'test') {
        // TODO: show more details about the test
        tooltipText = runnable.name;
        href = '';  // TODO: this
        // href = ChangesLinks.buildHref(build);
      } else {
        return <ProgrammingError>
          Unknown type {this.props.type}
        </ProgrammingError>;
      }

      return <SimpleTooltip label={tooltipText} placement="bottom">
        <a 
          className="chartBarColumn" 
          href={href}
          style={{ paddingTop: columnPadding }}>
          <div 
            className={"chartBar " + bgColor} 
            style={{ height: barHeight }}
          />
        </a>
      </SimpleTooltip>;
    });

    // add ellipses
    if (this.props.leftEllipsis) {
      content.unshift(<div className="inlineBlock marginRightXS">...</div>);
    } else if (this.props.enableLatest) {
      content.unshift(<LatestWidget />);
    }
    if (this.props.rightEllipsis) {
      content.push(<div className="inlineBlock">...</div>);
    }

    return <div className="changesChart">{content}</div>;
  }
});

var LatestWidget = React.createClass({
  render() {
    var divStyle = {
      position: 'absolute',
      marginLeft: -8,
      marginTop: -18,
      fontSize: 'xx-small',
    };

    var caretStyle = {
      display: 'block',
      marginLeft: 10,
      marginTop: -3
    };

    return <div style={divStyle}>
      Latest
      <i style={caretStyle} className="fa fa-caret-down" />
    </div>;
  }
});
